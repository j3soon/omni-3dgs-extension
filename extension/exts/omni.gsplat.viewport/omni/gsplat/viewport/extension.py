import threading

import numpy as np
import omni.ext
import omni.ui as ui
import omni.usd
import zmq
import torch as th
import warp as wp
import omni.replicator.core as rep
from omni.kit.viewport.utility import get_active_viewport, get_active_viewport_window
from omni.ui import scene as sc
from pxr import Gf, Usd, UsdGeom
from PIL import Image
from io import BytesIO


@wp.kernel
def normalize_depth(
    rgba: wp.array3d(dtype=wp.uint8),
    depth: wp.array2d(dtype=wp.float32),
    z_far: float,
):
    i, j = wp.tid()
    # Normalize depth to [0, 255]
    depth[i, j] = (min(depth[i, j], z_far) / z_far) * 255.0
    # Convert depth to uint8
    for c in range(3):
        rgba[i, j, c] = wp.uint8(depth[i, j])


# Any class derived from `omni.ext.IExt` in top level module (defined in `python.modules` of `extension.toml`) will be
# instantiated when extension gets enabled and `on_startup(ext_id)` will be called. Later when extension gets disabled
# on_shutdown() is called.
class OmniGSplatViewportExtension(omni.ext.IExt):
    # Name as omni.gsplat.viewport since omni.3dgs.viewport is not a valid name.

    def __init__(self):
        super().__init__()
        self.prev_camera_to_object_pos: Gf.Vec3d = None
        self.prev_camera_to_object_rot: Gf.Vec3d = None
        self.camera_to_object_pos: Gf.Vec3d = None
        self.camera_to_object_rot: Gf.Vec3d = None
        self.mesh_prim_path: str = None
        self.mesh_prim_visibility: str = None
        self.timeline_is_playing: bool = None
        # Replicator annotators
        self.rep_depth_annotator = None
        self.rep_rgba_annotator = None
        # Initialize ZMQ context and socket
        self.zmq_context = None
        self.zmq_socket = None
        # Initialize worker thread and event
        self.render_event = threading.Event()
        self.worker_thread = None
        self.should_stop = False
        # Only used when rendering depth
        self.z_far = 5

    # ext_id is current extension id. It can be used with extension manager to query additional information, like where
    # this extension is located on filesystem.
    def on_startup(self, ext_id):
        # To see the Python print output in Omniverse Code, open the `Script Editor`.
        # In Isaac Sim, see the startup console instead.
        print("[omni.gsplat.viewport] omni gsplat viewport startup")
        # Ref: https://docs.omniverse.nvidia.com/dev-guide/latest/programmer_ref/usd/stage/get-current-stage.html
        self.usd_context = omni.usd.get_context()
        # Subscribe to event streams
        # Ref: https://docs.omniverse.nvidia.com/kit/docs/kit-manual/latest/guide/event_streams.html
        # Ref: https://docs.omniverse.nvidia.com/kit/docs/kit-manual/104.0/carb.events/carb.events.IEventStream.html#carb.events.IEventStream.create_subscription_to_pop_by_type
        self.stage_event_stream = self.usd_context.get_stage_event_stream()
        self.stage_event_delegate = self.stage_event_stream.create_subscription_to_pop(
            self._on_stage_event, name="GSplat Viewport Stage Event"
        )
        # Listen to rendering events. Only triggered when the viewport is rendering is updated.
        # Will not be triggered when no viewport is visible on the screen.
        # Examples on using `get_rendering_event_stream` can be found by installing Isaac Sim
        # and searching for `get_rendering_event_stream` under `~/.local/share/ov/pkg/isaac_sim-2023.1.1`.
        self.rendering_event_stream = self.usd_context.get_rendering_event_stream()
        self.rendering_event_delegate = self.rendering_event_stream.create_subscription_to_pop(
            self._on_rendering_event, name="GSplat Viewport Rendering Event"
        )
        # Must use rendering events to trigger rendering, since update events are not synchronized with Replicator.
        # TODO: Consider subscribing to update events
        # Ref: https://docs.omniverse.nvidia.com/dev-guide/latest/programmer_ref/events.html#subscribe-to-update-events
        # Allocate memory
        self.rgba_w, self.rgba_h = 1280, 720 # Follow default camera resolution 1280x720
        self.rgba = th.ones((self.rgba_h, self.rgba_w, 4), dtype=th.uint8, device="cuda") * 128
        """RGBA image buffer. The shape is (H, W, 4), following the NumPy convention."""
        self.rgba[:,:,3] = 255
        self.rgba_rep = wp.zeros((self.rgba_h, self.rgba_w, 4), dtype=wp.uint8, device="cuda")
        self.depth_rep = wp.zeros((self.rgba_h, self.rgba_w), dtype=wp.float32, device="cuda")
        self.rgb_3dgs = th.zeros((self.rgba_h, self.rgba_w, 3), dtype=th.uint8, device="cuda")
        self.depth_3dgs = th.full((self.rgba_h, self.rgba_w), float('inf'), dtype=th.float32, device="cuda")
        # Init warp and disable verbose output
        wp.init()
        # Init ZMQ connection
        self.init_zmq()
        # Build UI
        self.build_ui(ext_id)
        self.timeline = omni.timeline.get_timeline_interface()
        # Start worker thread
        self.should_stop = False
        self.worker_thread = threading.Thread(target=self._render_worker, daemon=True)
        self.worker_thread.start()

    def init_zmq(self):
        """Initialize ZMQ connection"""
        self.zmq_context = zmq.Context()
        self.zmq_socket = self.zmq_context.socket(zmq.REQ)
        self.zmq_socket.connect("ipc:///tmp/omni-3dgs-extension/vanillags_renderer")

    def init_replicator(self):
        """Initialize Replicator connection"""
        viewport_api = get_active_viewport()
        cam_prim_path = viewport_api.camera_path.pathString
        self.render_product_path = rep.create.render_product(
            cam_prim_path,
            resolution=(self.rgba_w, self.rgba_h),
        ).path
        print(f"[omni.gsplat.viewport] Replicator render product path: {self.render_product_path}")
        self.rep_depth_annotator = rep.AnnotatorRegistry.get_annotator("distance_to_camera", device="cuda")
        self.rep_depth_annotator.attach(self.render_product_path)
        self.rep_rgba_annotator = rep.AnnotatorRegistry.get_annotator("LdrColor", device="cuda")
        self.rep_rgba_annotator.attach(self.render_product_path)

    def build_ui(self, ext_id):
        """Build the UI. Should be called upon startup."""
        # Please refer to the `Omni::UI Doc` tab in Omniverse Code for efficient development.
        # Ref: https://youtu.be/j1Pwi1KRkhk
        # Ref: https://github.com/NVIDIA-Omniverse
        # Ref: https://youtu.be/dNLFpVhBrGs
        self.ui_window = ui.Window("GSplat Viewport", width=self.rgba_w, height=self.rgba_h)

        with self.ui_window.frame:
            with ui.ZStack():
                # GSplat Viewport
                # Examples on using ByteImageProvider can be found by installing Isaac Sim
                # and searching for `set_bytes_data` under `~/.local/share/ov/pkg/isaac_sim-2023.1.1`.
                # Ref: https://docs.omniverse.nvidia.com/kit/docs/omni.ui/latest/omni.ui/omni.ui.ByteImageProvider.html
                # Ref: https://docs.omniverse.nvidia.com/kit/docs/omni.ui/latest/omni.ui/omni.ui.ImageWithProvider.html
                self.ui_3dgs_provider = ui.ByteImageProvider()
                self.ui_3dgs_img = ui.ImageWithProvider(
                    self.ui_3dgs_provider,
                    width=ui.Percent(100),
                    height=ui.Percent(100),
                )
                # TODO: Larger image size?
                with ui.VStack(height=0):
                    # UI for setting the 3DGS mesh
                    # Ref: https://docs.omniverse.nvidia.com/workflows/latest/extensions/scatter_tool.html
                    with ui.HStack():
                        self.ui_lbl_mesh = ui.Label("3DGS Mesh", width=65)
                        # Ref: https://docs.omniverse.nvidia.com/dev-guide/latest/programmer_ref/ui/widgets/stringfield.html
                        self._mesh_prim_model = ui.SimpleStringModel()
                        ui.StringField(model=self._mesh_prim_model)
                        ui.Button(
                            " S ",
                            width=0,
                            height=0,
                            clicked_fn=self._on_btn_set_click,
                            tooltip="Get From Selection",
                        )
                    ui.Button("Reset Camera", width=20, clicked_fn=self._on_btn_reset_click)
                    with ui.HStack():
                        ui.Label("Viewport Overlay", width=100)
                        model = ui.CheckBox().model
                        model.add_value_changed_fn(self._on_checkbox_value_changed)

        # Camera Viewport
        # Ref: https://docs.omniverse.nvidia.com/kit/docs/omni.kit.viewport.docs/latest/overview.html#simplest-example
        # Don't create a new viewport widget as below, since the viewport widget will often flicker.
        # Ref: https://docs.omniverse.nvidia.com/dev-guide/latest/release-notes/known-limits.html
        # ```
        # from omni.kit.widget.viewport import ViewportWidget
        # self.ui_viewport_widget = ViewportWidget(
        #     resolution = (640, 360),
        #     width = 640,
        #     height = 360,
        # )
        # self.viewport_api = self.ui_viewport_widget.viewport_api
        # ````
        # Ref: https://docs.omniverse.nvidia.com/dev-guide/latest/python-snippets/viewport/change-viewport-active-camera.html
        # Instead, the viewport is obtained from the active viewport in new renderings.
        self.viewport_window = get_active_viewport_window()
        with self.viewport_window.get_frame(ext_id):
            self.scene_view = sc.SceneView(
                screen_aspect_ratio=self.rgba_w/self.rgba_h,
            )
        self.configure_viewport_overlay(False)

        self.update_ui()

    def update_ui(self):
        print("[omni.gsplat.viewport] Updating UI")
        # Ref: https://forums.developer.nvidia.com/t/refresh-window-ui/221200
        self.ui_window.frame.rebuild()

    def configure_viewport_overlay(self, show_overlay: bool):
        print(f"[omni.gsplat.viewport] Configuring viewport overlay: {show_overlay}")
        if not show_overlay:
            if self.scene_view is not None:
                self.scene_view.scene.clear()
            return
        # if show_overlay, then populate the scene view
        with self.scene_view.scene:
            # Screen coordinates are in [-1, 1]
            # Ref: https://docs.omniverse.nvidia.com/workflows/latest/extensions/viewport_reticle.html
            sc.Image(
                self.ui_3dgs_provider,
                width=2,
                height=2,
            )

    def _on_btn_set_click(self):
        self._mesh_prim_model.as_string = self._get_selected_prim_path()

    def _on_btn_reset_click(self):
        # TODO: Allow resetting the camera to a specific position
        # Below doesn't seem to work
        # stage: Usd.Stage = self.usd_context.get_stage()
        # prim: Usd.Prim = stage.GetPrimAtPath('/OmniverseKit_Persp')
        # # `UsdGeom.Xformable(prim).SetTranslateOp` doesn't seem to exist
        # prim.GetAttribute("xformOp:translate").Set(Gf.Vec3d(0, 0, 0.1722))
        # prim.GetAttribute("xformOp:rotateXYZ").Set(Gf.Vec3d(0, -152, 0))
        # print("translateOp", prim.GetAttribute("xformOp:translate").Get())
        # print("rotateXYZOp", prim.GetAttribute("xformOp:rotateXYZ").Get())
        print("[omni.gsplat.viewport] (TODO) Reset Camera")

    def _on_checkbox_value_changed(self, model):
        value = model.get_value_as_bool()
        self.configure_viewport_overlay(value)

    def _get_selected_prim_path(self):
        """Get the selected prim. Return '' if no prim is selected."""
        # Ref: https://docs.omniverse.nvidia.com/workflows/latest/extensions/object_info.html#step-5-get-the-selected-prims-data
        selected_prim_paths = self.usd_context.get_selection().get_selected_prim_paths()
        if not selected_prim_paths:
            return ''
        return selected_prim_paths[0]

    def _get_camera_pose(self):
        viewport_api = get_active_viewport()
        # We chose to use Viewport instead of Isaac Sim's Camera Sensor to avoid dependency on Isaac Sim.
        # We want the extension to work with any Omniverse app, not just Isaac Sim.
        # Ref: https://docs.omniverse.nvidia.com/isaacsim/latest/features/sensors_simulation/isaac_sim_sensors_camera.html
        camera_to_world_mat: Gf.Matrix4d = viewport_api.transform
        object_to_world_mat: Gf.Matrix4d = Gf.Matrix4d()
        if self._mesh_prim_model.as_string != '':
            stage: Usd.Stage = self.usd_context.get_stage()
            # meters_per_unit = UsdGeom.GetStageMetersPerUnit(stage)
            selected_prim: Usd.Prim = stage.GetPrimAtPath(self._mesh_prim_model.as_string)
            selected_xform: UsdGeom.Xformable = UsdGeom.Xformable(selected_prim)
            object_to_world_mat = selected_xform.GetLocalTransformation()
        # In USD, pre-multiplication is used for matrices.
        # Ref: https://openusd.org/dev/api/usd_geom_page_front.html#UsdGeom_LinAlgBasics
        world_to_object_mat: Gf.Matrix4d = object_to_world_mat.GetInverse()
        camera_to_object_mat: Gf.Matrix4d = camera_to_world_mat * world_to_object_mat
        camera_to_object_pos: Gf.Vec3d = camera_to_object_mat.ExtractTranslation()
        # I suspect that the `Decompose` function will extract the rotation in the order of the input axes.
        # So for EulerXYZ, we want to first extract and remove the Z rotation, then Y, then X.
        # Then we reverse the order to get the XYZ rotation.
        # I haven't spend time looking into the source code to confirm this hypothesis though.
        # Ref: https://forums.developer.nvidia.com/t/how-to-get-euler-angle-of-the-prim-through-script-with-script-editor/269704/3
        # Ref: https://github.com/PixarAnimationStudios/OpenUSD/blob/2864f3d04f396432f22ec5d6928fc37d34bb4c90/pxr/base/gf/rotation.cpp#L108
        # must remove scale before rotation
        camera_to_object_mat.Orthonormalize()
        camera_to_object_rot: Gf.Vec3d = Gf.Vec3d(*reversed(camera_to_object_mat.ExtractRotation().Decompose(*reversed(Gf.Matrix3d()))))
        # TODO: Consider using viewport camera projection matrix `viewport_api.projection`?
        # Not same as below due to the potential difference in rotation matrix representation
        # ```
        # from scipy.spatial.transform import Rotation as R
        # camera_rotation: Gf.Vec3d = R.from_matrix(camera_mat.ExtractRotationMatrix()).as_euler('xyz', degrees=True) # in degrees
        # ```
        return camera_to_object_pos, camera_to_object_rot

    def _fill_3dgs_buffers(self):
        if self.mesh_prim_path == '':
            return
        camera_to_object_pos, camera_to_object_rot = self.camera_to_object_pos, self.camera_to_object_rot
        # Uncomment for Eco Mode
        # if camera_to_object_pos == self.prev_camera_to_object_pos and camera_to_object_rot == self.prev_camera_to_object_rot:
        #     return
        self.prev_camera_to_object_pos = camera_to_object_pos
        self.prev_camera_to_object_rot = camera_to_object_rot
        
        # Prepare camera pose data
        pose_data = {
            'position': list(camera_to_object_pos),
            'rotation': list(np.deg2rad(camera_to_object_rot))
        }

        # Convert rgba_rep and depth_rep to numpy arrays
        rgb_np = (wp.to_torch(self.rgba_rep)[:,:,:3]).cpu().numpy()  # Convert to numpy array
        depth_np = wp.to_torch(self.depth_rep).cpu().numpy()  # Convert to numpy array

        # Convert numpy arrays to PIL Images
        rgb_img = Image.fromarray(rgb_np.astype(np.uint8))
        depth_img = Image.fromarray(depth_np, mode='F')  # 'F' mode for float32

        # Compress as TIFF
        rgb_buffer = BytesIO()
        depth_buffer = BytesIO()
        rgb_img.save(rgb_buffer, format='TIFF')
        depth_img.save(depth_buffer, format='TIFF')

        # Send multipart message
        self.zmq_socket.send_json(pose_data, zmq.SNDMORE)
        self.zmq_socket.send(rgb_buffer.getvalue(), zmq.SNDMORE)
        self.zmq_socket.send(depth_buffer.getvalue())
        
        # Receive metadata and image data separately
        metadata = self.zmq_socket.recv_json()
        render_bytes = self.zmq_socket.recv()
        inv_depth_bytes = self.zmq_socket.recv()
        
        if 'error' in metadata:
            print(f"[omni.gsplat.viewport] Error from server: {metadata['error']}")
        else:
            # Decompress render image
            render_buffer = BytesIO(render_bytes)
            render_img = Image.open(render_buffer)
            render_np = np.array(render_img) # HWC
            self.rgb_3dgs[:] = th.from_numpy(render_np).to("cuda") # HWC

            # Decompress inverse depth image
            inv_depth_buffer = BytesIO(inv_depth_bytes)
            inv_depth_img = Image.open(inv_depth_buffer)
            inv_depth_np = np.array(inv_depth_img) # HW
            self.depth_3dgs[:] = 1 / th.from_numpy(inv_depth_np).to("cuda") # HW

    def _render_worker(self):
        """Worker thread that processes render requests when event is set"""
        print("[omni.gsplat.viewport] Render worker started")
        th.set_grad_enabled(False) # disable gradient calculation
        # Note that we don't touch UI in the worker thread
        while not self.should_stop:
            # Wait for render event
            self.render_event.wait()
            # Check data shape since it may be (0,) during initialization
            if not self.timeline_is_playing or \
                self.rep_depth_annotator is None or \
                self.rep_rgba_annotator is None or \
                self.depth_rep.shape != (self.rgba_h, self.rgba_w) or \
                self.rgba_rep.shape != (self.rgba_h, self.rgba_w, 4):
                # Don't use background image feature if not available
                self.rgba_rep = wp.from_torch(th.zeros((self.rgba_h, self.rgba_w, 4), dtype=th.uint8, device="cuda"))
                self.depth_rep = wp.from_torch(th.full((self.rgba_h, self.rgba_w), float('inf'), dtype=th.float32, device="cuda"))
            try:
                # No need to check event type, since there is only one event type: `NEW_FRAME`.
                self._fill_3dgs_buffers()
            except Exception as e:
                print(f"[omni.gsplat.viewport] Error in render worker: {e}")
            self._update_and_frame_buffer()
            # Signal after rendering is done
            self.render_event.clear()
        print("[omni.gsplat.viewport] Render worker stopped")

    def _set_rgba_to_depth(self):
        wp.launch(
            normalize_depth,
            dim=(self.rgba_h, self.rgba_w),
            inputs=[wp.from_torch(self.rgba), self.depth_rep, self.z_far],
            device="cuda",
        )
        # Same concept as cudaDeviceSynchronize()
        wp.synchronize()
        # Uncomment below to show replicator image
        # self.rgba[:,:,:3] = wp.to_torch(self.rgba_rep)[:,:,:3]

    def _update_and_frame_buffer(self):
        if self.mesh_prim_path == '' and not self.timeline_is_playing:
            # If no mesh is selected, render the dummy image.
            self.rgba[:,:,:3] = ((self.rgba[:,:,:3].int() + 1) % 256).to(th.uint8)
            return
        self.rgba[:,:,3] = 255

        if self.mesh_prim_visibility != "invisible" and self.timeline_is_playing:
            self._set_rgba_to_depth()
            return

        self.rgba[:,:,:3] = self.rgb_3dgs

    def _on_stage_event(self, event):
        """Called by stage_event_stream."""
        if event.type == int(omni.usd.StageEventType.OPENED):
            print(f"[omni.gsplat.viewport] Stage Opened")
        elif event.type == int(omni.usd.StageEventType.CLOSING):
            print(f"[omni.gsplat.viewport] Stage Closing")
            self._mesh_prim_model.as_string = ''
            self._cleanup()
        # Uncomment for Eco Mode
        # elif event.type == int(omni.usd.StageEventType.ASSETS_LOADED):
        #     print(f"[omni.gsplat.viewport] Assets Loaded")
        #     # Invalidate the previous camera pose to force redraw
        #     self.prev_camera_to_object_pos = None
        #     self.prev_camera_to_object_rot = None

    def _on_rendering_event(self, event):
        """Called by rendering_event_stream."""
        if self.rep_depth_annotator is None:
            self.init_replicator()
        if self.render_event.is_set():
            return
        # Update UI to show the rendered image of the previous render event
        # Know issues:
        # The image may flicker (red) for a single frame
        # - after stopping the timeline while the mesh prim is visible.
        # - after making the mesh prim invisible while the timeline is playing.
        self.ui_3dgs_provider.set_bytes_data_from_gpu(self.rgba.data_ptr(), (self.rgba_w, self.rgba_h))

        # Prepare data for the next render event
        # Get all scene-related data in the main (UI) thread to prevent race condition and synchronization issues
        # Get Replicator data
        self.depth_rep = self.rep_depth_annotator.get_data() # is warp array with shape (H, W)
        self.rgba_rep = self.rep_rgba_annotator.get_data() # is warp array with shape (H, W, 4)
        # Get camera pose
        self.camera_to_object_pos, self.camera_to_object_rot = self._get_camera_pose()
        self.mesh_prim_path = self._mesh_prim_model.as_string
        if self.mesh_prim_path != '':
            prim: Usd.Prim = self.usd_context.get_stage().GetPrimAtPath(self.mesh_prim_path)
            self.mesh_prim_visibility = prim.GetAttribute("visibility").Get()
        self.timeline_is_playing = self.timeline.is_playing()
        # Signal to render worker to start rendering
        self.render_event.set()

    def _cleanup(self):
        # Detach Replicator depth annotator
        self.timeline.stop()
        if self.rep_depth_annotator is not None:
            self.rep_depth_annotator.detach([self.render_product_path])
        if self.rep_rgba_annotator is not None:
            self.rep_rgba_annotator.detach([self.render_product_path])
        self.rep_depth_annotator = None
        self.rep_rgba_annotator = None
        self.configure_viewport_overlay(False)

    def on_shutdown(self):
        print("[omni.gsplat.viewport] omni gsplat viewport shutdown")
        # Stop worker thread
        self.should_stop = True
        self.render_event.set()  # Wake up worker thread to check should_stop
        if self.worker_thread is not None:
            self.worker_thread.join(timeout=1.0)
        if self.zmq_socket:
            self.zmq_socket.close()
        if self.zmq_context:
            self.zmq_context.term()
        self._cleanup()

    def destroy(self):
        # Ref: https://docs.omniverse.nvidia.com/workflows/latest/extensions/object_info.html#step-3-4-use-usdcontext-to-listen-for-selection-changes
        self.rendering_event_stream = None
        self.rendering_event_delegate.unsubscribe()
