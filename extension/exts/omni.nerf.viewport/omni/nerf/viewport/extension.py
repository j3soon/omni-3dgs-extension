import platform
import threading

import cv2
import numpy as np
import omni.ext
import omni.ui as ui
import omni.usd
import zmq
import base64
import torch as th
from omni.kit.viewport.utility import get_active_viewport, get_active_viewport_window
from omni.ui import scene as sc
from pxr import Gf, Usd, UsdGeom


# Functions and vars are available to other extension as usual in python: `example.python_ext.some_public_function(x)`
def some_public_function(x: int):
    print("[omni.nerf.viewport] some_public_function was called with x: ", x)
    return x ** x


# Any class derived from `omni.ext.IExt` in top level module (defined in `python.modules` of `extension.toml`) will be
# instantiated when extension gets enabled and `on_startup(ext_id)` will be called. Later when extension gets disabled
# on_shutdown() is called.
class OmniNerfViewportExtension(omni.ext.IExt):

    def __init__(self):
        super().__init__()
        self.is_python_supported: bool = platform.python_version().startswith("3.10")
        """The Python version must match the backend version for RPyC to work."""
        self.camera_position: Gf.Vec3d = None
        self.camera_rotation: Gf.Vec3d = None
        # Initialize ZMQ context and socket
        self.zmq_context = None
        self.zmq_socket = None
        # Initialize worker thread and event
        self.render_event = threading.Event()
        self.worker_thread = None
        self.should_stop = False

    # ext_id is current extension id. It can be used with extension manager to query additional information, like where
    # this extension is located on filesystem.
    def on_startup(self, ext_id):
        # To see the Python print output in Omniverse Code, open the `Script Editor`.
        # In Isaac Sim, see the startup console instead.
        print("[omni.nerf.viewport] omni nerf viewport startup")
        # Ref: https://docs.omniverse.nvidia.com/dev-guide/latest/programmer_ref/usd/stage/get-current-stage.html
        self.usd_context = omni.usd.get_context()
        # Subscribe to event streams
        # Ref: https://docs.omniverse.nvidia.com/kit/docs/kit-manual/latest/guide/event_streams.html
        # Ref: https://docs.omniverse.nvidia.com/kit/docs/kit-manual/104.0/carb.events/carb.events.IEventStream.html#carb.events.IEventStream.create_subscription_to_pop_by_type
        # Listen to rendering events. Only triggered when the viewport is rendering is updated.
        # Will not be triggered when no viewport is visible on the screen.
        # Examples on using `get_rendering_event_stream` can be found by installing Isaac Sim
        # and searching for `get_rendering_event_stream` under `~/.local/share/ov/pkg/isaac_sim-2023.1.1`.
        self.rendering_event_stream = self.usd_context.get_rendering_event_stream()
        self.rendering_event_delegate = self.rendering_event_stream.create_subscription_to_pop(
            self._on_rendering_event, name="NeRF Viewport Update"
        )
        # TODO: Consider subscribing to update events
        # Ref: https://docs.omniverse.nvidia.com/dev-guide/latest/programmer_ref/events.html#subscribe-to-update-events
        # Allocate memory
        self.rgba_w, self.rgba_h = 1280, 720 # Follow default camera resolution 1280x720
        self.rgba = th.ones((self.rgba_h, self.rgba_w, 4), dtype=th.uint8, device="cuda") * 128
        """RGBA image buffer. The shape is (H, W, 4), following the NumPy convention."""
        self.rgba[:,:,3] = 255
        # Init ZMQ connection
        if self.is_python_supported:
            self.init_zmq()
        # Build UI
        self.build_ui(ext_id)
        # Start worker thread
        self.should_stop = False
        self.worker_thread = threading.Thread(target=self._render_worker, daemon=True)
        self.worker_thread.start()

    def init_zmq(self):
        """Initialize ZMQ connection"""
        self.zmq_context = zmq.Context()
        self.zmq_socket = self.zmq_context.socket(zmq.REQ)
        self.zmq_socket.connect("ipc:///tmp/omni-3dgs-extension/vanillags_renderer")

    def build_ui(self, ext_id):
        """Build the UI. Should be called upon startup."""
        # Please refer to the `Omni::UI Doc` tab in Omniverse Code for efficient development.
        # Ref: https://youtu.be/j1Pwi1KRkhk
        # Ref: https://github.com/NVIDIA-Omniverse
        # Ref: https://youtu.be/dNLFpVhBrGs
        self.ui_window = ui.Window("NeRF Viewport", width=self.rgba_w, height=self.rgba_h)

        with self.ui_window.frame:
            with ui.ZStack():
                # NeRF Viewport
                # Examples on using ByteImageProvider can be found by installing Isaac Sim
                # and searching for `set_bytes_data` under `~/.local/share/ov/pkg/isaac_sim-2023.1.1`.
                # Ref: https://docs.omniverse.nvidia.com/kit/docs/omni.ui/latest/omni.ui/omni.ui.ByteImageProvider.html
                # Ref: https://docs.omniverse.nvidia.com/kit/docs/omni.ui/latest/omni.ui/omni.ui.ImageWithProvider.html
                self.ui_nerf_provider = ui.ByteImageProvider()
                self.ui_nerf_img = ui.ImageWithProvider(
                    self.ui_nerf_provider,
                    width=ui.Percent(100),
                    height=ui.Percent(100),
                )
                # TODO: Larger image size?
                with ui.VStack(height=0):
                    self.ui_lbl_py = ui.Label("(To Be Updated)")
                    state = "supported" if platform.python_version().startswith("3.10") else "NOT supported"
                    self.ui_lbl_py.text = f"Python {platform.python_version()} is {state}"
                    # UI for setting the NeRF mesh
                    # Ref: https://docs.omniverse.nvidia.com/workflows/latest/extensions/scatter_tool.html
                    with ui.HStack():
                        self.ui_lbl_mesh = ui.Label("NeRF Mesh", width=65)
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
        print("[omni.nerf.viewport] Updating UI")
        # Ref: https://forums.developer.nvidia.com/t/refresh-window-ui/221200
        self.ui_window.frame.rebuild()

    def configure_viewport_overlay(self, show_overlay: bool):
        print(f"[omni.nerf.viewport] Configuring viewport overlay: {show_overlay}")
        if not show_overlay:
            if self.scene_view is not None:
                self.scene_view.scene.clear()
            return
        # if show_overlay, then populate the scene view
        with self.scene_view.scene:
            # Screen coordinates are in [-1, 1]
            # Ref: https://docs.omniverse.nvidia.com/workflows/latest/extensions/viewport_reticle.html
            sc.Image(
                self.ui_nerf_provider,
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
        print("[omni.nerf.viewport] (TODO) Reset Camera")

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

    def _render_worker(self):
        """Worker thread that processes render requests when event is set"""
        print("[omni.nerf.viewport] Render worker started")
        th.set_grad_enabled(False) # disable gradient calculation
        while not self.should_stop:
            # Wait for render event
            self.render_event.wait()
            self.render_event.clear()
            try:
                # No need to check event type, since there is only one event type: `NEW_FRAME`.
                if self.is_python_supported and self._mesh_prim_model.as_string != '':
                    viewport_api = get_active_viewport()
                    # We chose to use Viewport instead of Isaac Sim's Camera Sensor to avoid dependency on Isaac Sim.
                    # We want the extension to work with any Omniverse app, not just Isaac Sim.
                    # Ref: https://docs.omniverse.nvidia.com/isaacsim/latest/features/sensors_simulation/isaac_sim_sensors_camera.html
                    camera_to_world_mat: Gf.Matrix4d = viewport_api.transform
                    object_to_world_mat: Gf.Matrix4d = Gf.Matrix4d()
                    if self._mesh_prim_model.as_string != '':
                        stage: Usd.Stage = self.usd_context.get_stage()
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
                    # TODO: Consider object transform (if it is moved or rotated)
                    # No need to transform from Isaac Sim space to Nerfstudio space, since they are both in the same space.
                    # Ref: https://github.com/j3soon/coordinate-system-conventions
                    if camera_to_object_pos != self.camera_position or camera_to_object_rot != self.camera_rotation:
                        self.camera_position = camera_to_object_pos
                        self.camera_rotation = camera_to_object_rot
                        print("[omni.nerf.viewport] New camera position:", camera_to_object_pos)
                        print("[omni.nerf.viewport] New camera rotation:", camera_to_object_rot)
                        
                        # Prepare camera pose data
                        pose_data = {
                            'position': list(camera_to_object_pos),
                            'rotation': list(np.deg2rad(camera_to_object_rot))
                        }
                        self.zmq_socket.send_json(pose_data)
                        response = self.zmq_socket.recv_json()
                        
                        if 'error' in response:
                            print(f"[omni.nerf.viewport] Error from server: {response['error']}")
                        else:
                            # Convert base64 string back to numpy array
                            shape = response['shape'] # HWC
                            image_base64 = response['image']
                            image_bytes = base64.b64decode(image_base64)
                            image = np.frombuffer(image_bytes, dtype=np.uint8).reshape(shape)
                            
                            # Resize to match viewport dimensions
                            image = cv2.resize(image, (self.rgba_w, self.rgba_h), interpolation=cv2.INTER_LINEAR)
                            print("[omni.nerf.viewport] NeRF viewport updated")
                            self.rgba[:,:,:3] = th.from_numpy(image).to(device="cuda")
                else:
                    # If python version is not supported, render the dummy image.
                    self.rgba[:,:,:3] = ((self.rgba[:,:,:3].int() + 1) % 256).to(th.uint8)
                self.ui_nerf_provider.set_bytes_data_from_gpu(self.rgba.data_ptr(), (self.rgba_w, self.rgba_h))
            except Exception as e:
                print(f"[omni.nerf.viewport] Error in render worker: {e}")
        print("[omni.nerf.viewport] Render worker stopped")

    def _on_rendering_event(self, event):
        """Called by rendering_event_stream."""
        self.render_event.set()

    def on_shutdown(self):
        print("[omni.nerf.viewport] omni nerf viewport shutdown")
        # Stop worker thread
        self.should_stop = True
        self.render_event.set()  # Wake up worker thread to check should_stop
        if self.worker_thread is not None:
            self.worker_thread.join(timeout=1.0)
        if self.is_python_supported:
            if self.zmq_socket:
                self.zmq_socket.close()
            if self.zmq_context:
                self.zmq_context.term()
        self.configure_viewport_overlay(False)

    def destroy(self):
        # Ref: https://docs.omniverse.nvidia.com/workflows/latest/extensions/object_info.html#step-3-4-use-usdcontext-to-listen-for-selection-changes
        self.stage_event_stream = None
        self.stage_event_delegate.unsubscribe()
