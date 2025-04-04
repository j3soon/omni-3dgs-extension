"""
Note that this file is based on the following two references:
* https://github.com/shumash/gaussian-splatting/blob/1616419cda09a0e0249a6ab6c2d10e44f9e1c2ea/interactive.ipynb
* https://github.com/graphdeco-inria/gaussian-splatting/tree/54c035f7834b564019656c3e3fcc3646292f727d
"""

import sys
import numpy as np
import torch
import zmq
from PIL import Image
from scipy.spatial.transform import Rotation
from io import BytesIO

# Assume running in the pre-built gaussian-splatting container
sys.path.append('/workspace/gaussian-splatting')

from gaussian_renderer import render
from scene.gaussian_model import GaussianModel
from scene.cameras import Camera as GSCamera


class PipelineParamsNoparse:
    """ Same as PipelineParams but without argument parser. """
    def __init__(self):
        self.convert_SHs_python = False
        self.compute_cov3D_python = False
        self.debug = False
        self.antialiasing = False

def create_camera_from_pose(position, euler_angles, width=1280, height=720):
    # The default width and height is the default resolution of Isaac Sim

    C2W = np.eye(4)
    C2W[:3, :3] = Rotation.from_euler('xyz', euler_angles).as_matrix()
    C2W[:3, 3] = position
    W2C = np.linalg.inv(C2W)
    ISAAC_SIM_TO_GS_CONVENTION = np.array([
        [1,  0,  0, 0],
        [0, -1,  0, 0],
        [0,  0, -1, 0],
        [0,  0,  0, 1]
    ])
    # Convert from Isaac Sim to GS camera convention
    # - Isaac Sim: +X Right, +Y Up, -Z Forward
    #   https://docs.omniverse.nvidia.com/isaacsim/latest/reference_conventions.html#default-camera-axes
    # - GS/COLMAP: +X Right, -Y Up, +Z Forward
    #   https://github.com/graphdeco-inria/gaussian-splatting/issues/100#issuecomment-1686463391
    # This conversion must be done on W2C, not C2W, so as to rotate around the camera center.
    W2C = ISAAC_SIM_TO_GS_CONVENTION @ W2C
    # Following the COLMAP convention:
    # - https://colmap.github.io/format.html#images-txt
    #   - R is camera to world rotation
    #   - T is world to camera translation
    R = W2C[:3, :3].T
    T = W2C[:3, 3]
    # Other references:
    # - https://github.com/graphdeco-inria/gaussian-splatting/blob/54c035f7834b564019656c3e3fcc3646292f727d/utils/graphics_utils.py#L38-L49
    # - https://github.com/graphdeco-inria/gaussian-splatting/blob/54c035f7834b564019656c3e3fcc3646292f727d/scene/cameras.py#L86-L89
    # - https://github.com/graphdeco-inria/gaussian-splatting/blob/54c035f7834b564019656c3e3fcc3646292f727d/utils/camera_utils.py#L78-L85
    #   I think the `W2C` variable in the code above should be renamed to `C2W`? I'm not entirely sure though.
    # - https://github.com/graphdeco-inria/gaussian-splatting/blob/54c035f7834b564019656c3e3fcc3646292f727d/scene/dataset_readers.py#L239-L247

    # Isaac Sim camera defaults:
    # - Size: 1280x720
    # - Focal Length: 18.14756
    # - Horizontal Aperture: 20.955
    # - Vertical Aperture: (Value Unused)
    # - (Calculated) horizontal FoV = math.degrees(2 * math.atan(20.955 / (2 * 18.14756))) = 60
    # - (Calculated) vertical FoV = math.degrees(2 * math.atan((height / width) * math.tan(math.radians(fov_horizontal) / 2))) = 35.98339777135764
    # Some useful equations:
    # - focal_length = width / (2 * math.tan(math.radians(fov_horizontal) / 2))
    # - focal_length = height / (2 * math.tan(math.radians(fov_vertical) / 2))
    # - fov_vertical = math.degrees(2 * math.atan(height / (2 * focal_length)))
    # - fov_horizontal = math.degrees(2 * math.atan(width / (2 * focal_length)))
    # - fov_horizontal = math.degrees(2 * math.atan(horiz_aperture / (2 * focal_length)))
    #   Ref: https://forums.developer.nvidia.com/t/change-intrinsic-camera-parameters/180309/6
    # - aspect_ratio = width / height
    # - fov_vertical = math.degrees(2 * math.atan((height / width) * math.tan(math.radians(fov_horizontal) / 2)))
    # Follow the default camera parameters in Isaac Sim
    fovx = np.radians(60)
    fovy = np.radians(35.98339777135764)

    image = Image.new('RGB', (width, height))  # fake image
    return GSCamera(
        resolution=image.size, 
        colmap_id=0,
        R=R, 
        T=T, 
        FoVx=fovx, 
        FoVy=fovy,
        depth_params=None,
        image=image,
        invdepthmap=None, 
        image_name='fake', 
        uid=0
    )

def main():
    # TODO: Make the socket url and checkpoint path configurable
    # Initialize ZMQ
    context = zmq.Context()
    receiver = context.socket(zmq.REP)
    receiver.bind("ipc:///tmp/omni-3dgs-extension/vanillags_renderer")
    
    # Load 3DGS model
    checkpt_path = "/workspace/data/exports/poster/splatfacto/DATE_TIME/splat/splat.ply"
    gaussians = GaussianModel(sh_degree=3)
    gaussians.load_ply(checkpt_path)
    pipeline = PipelineParamsNoparse()
    background = torch.tensor([0, 0, 0], dtype=torch.float32, device="cuda")
    bg_rgb = torch.ones((3, 720, 1280), dtype=torch.float32, device="cuda") * torch.tensor([1.0, 0.0, 0.0], device="cuda").reshape(3, 1, 1)
    bg_depth = torch.full((720, 1280), float('inf'), dtype=torch.float32, device="cuda")
    
    print("Gaussian Splatting renderer ready for requests...")
    
    while True:
        try:
            # Receive multipart message
            metadata = receiver.recv_json()
            bg_rgb_data = receiver.recv()
            bg_depth_data = receiver.recv()
            # Decompress TIFF data
            bg_rgb_img = Image.open(BytesIO(bg_rgb_data))
            bg_depth_img = Image.open(BytesIO(bg_depth_data))
            # Convert to numpy then to torch tensor
            bg_rgb_np = np.array(bg_rgb_img)  # HWC
            bg_depth_np = np.array(bg_depth_img)
            # Update background RGB and depth
            bg_rgb = torch.from_numpy(bg_rgb_np).float().to("cuda").permute(2, 0, 1) / 255  # Convert HWC to CHW
            bg_depth = torch.from_numpy(bg_depth_np).float().to("cuda")

            # Render
            camera = create_camera_from_pose(
                np.array(metadata['position']), 
                np.array(metadata['rotation'])
            )
            render_res = render(camera, gaussians, pipeline, background, bg_rgb, bg_depth)

            # Convert from CHW (torch) to HWC (numpy)
            # Need to ensure array is C contiguous before JPEG encoding
            render_np = (render_res["render"].permute(1, 2, 0) * 255).to(torch.uint8).detach().cpu(memory_format=torch.contiguous_format).numpy()
            # The "depth" here actually contains the inverse depth
            # Ref: https://github.com/graphdeco-inria/diff-gaussian-rasterization/blob/9c5c2028f6fbee2be239bc4c9421ff894fe4fbe0/rasterize_points.cu#L123
            inv_depth_np = render_res["depth"].permute(1, 2, 0).detach().cpu(memory_format=torch.contiguous_format).numpy()
            # Convert numpy arrays to PIL Images and compress as TIFF
            render_img = Image.fromarray(render_np)
            inv_depth_img = Image.fromarray(inv_depth_np.squeeze(), mode='F')  # 'F' mode for float32
            # Save to bytes buffer
            render_buffer = BytesIO()
            inv_depth_buffer = BytesIO()
            render_img.save(render_buffer, format='TIFF')
            inv_depth_img.save(inv_depth_buffer, format='TIFF')
            compressed_render = render_buffer.getvalue()
            compressed_inv_depth = inv_depth_buffer.getvalue()
            
            # Send metadata first
            metadata = {'shape': render_np.shape}
            receiver.send_json(metadata, zmq.SNDMORE)
            # Send compressed render image
            receiver.send(compressed_render, zmq.SNDMORE)
            # Send compressed inverse depth image
            receiver.send(compressed_inv_depth)
        except Exception as e:
            print(f"Error during rendering: {e}")
            # Send error response
            receiver.send_json({'error': str(e)})

if __name__ == "__main__":
    main()
