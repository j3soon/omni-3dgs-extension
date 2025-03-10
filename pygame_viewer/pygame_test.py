import argparse
import time

import cv2
import numpy as np
import pygame
import zmq
from PIL import Image
import io


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--socket-url', type=str, 
                        default="ipc:///tmp/omni-3dgs-extension/vanillags_renderer",
                        help="ZMQ socket URL to connect to")
    args = parser.parse_args()
    return args

def main(args):
    # Initialize ZMQ
    context = zmq.Context()
    socket = context.socket(zmq.REQ)
    socket.connect(args.socket_url)

    # Initialize Pygame
    pygame.init()

    # Set the width and height of the window
    width, height = 640, 360
    window_size = (width, height)

    # Create a Pygame window
    screen = pygame.display.set_mode(window_size)

    # Create a clock to control the frame rate
    clock = pygame.time.Clock()

    # Camera curve time & global screen buffer
    camera_curve_time = 0
    screen_buffer = np.zeros((width, height, 3), dtype=np.uint8)

    # Camera pose for the poster 3DGS model
    camera_position = [0, 0, 0]
    camera_rotation = [0, 0, 0]

    running = True
    while running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False

        # Prepare camera pose data
        pose_data = {
            'position': camera_position,
            'rotation': camera_rotation
        }

        try:
            socket.send_json(pose_data)
            # Receive multipart response
            metadata = socket.recv_json()
            compressed_render = socket.recv()
            compressed_depth = socket.recv()

            if 'error' in metadata:
                print(f"Error from server: {metadata['error']}")
            else:
                # Decode TIFF images using PIL
                render_img = Image.open(io.BytesIO(compressed_render))
                depth_img = Image.open(io.BytesIO(compressed_depth))
                
                # Convert to numpy arrays
                render_np = np.array(render_img) # HWC
                depth_np = np.array(depth_img) # HW
                image = render_np
                # Uncomment below to see depth image
                # z_far = 5
                # normalized_depth = 1 - np.minimum(depth_np, z_far) / z_far
                # image = (normalized_depth * 255)[..., np.newaxis].repeat(3, axis=-1)

                # Resize and process image
                image = cv2.resize(image, (width, height), interpolation=cv2.INTER_LINEAR).transpose(1, 0, 2)
                screen_buffer[:] = image

        except Exception as e:
            print(f"Error during communication: {e}")

        animation_progress = (np.sin(camera_curve_time) + 1) / 2

        # Cover the screen buffer with an indicator of camera position
        hud_width, hud_height = 100, 50
        bar_x, bar_y = 20, 24
        bar_w, bar_h = 60, 2
        # white background
        camera_position_indicator = np.ones((hud_width, hud_height, 3)) * 255
        # horizontal line
        camera_position_indicator[bar_x:bar_x+bar_w, bar_y:bar_y+bar_h, :] = 0
        # square indicator of current position
        hud_x = round(bar_x + bar_w * animation_progress)
        camera_position_indicator[hud_x-5:hud_x+5, 20:30, :] = 0
        screen_buffer[width-hud_width:, height-hud_height:, :] = camera_position_indicator

        # Convert the NumPy array to a Pygame surface
        image_surface = pygame.surfarray.make_surface(screen_buffer)

        # Blit the surface to the screen
        screen.blit(image_surface, (0, 0))
        pygame.display.flip()

        # Control the frame rate
        clock.tick(30)

        # Move Camera
        camera_position[2] = animation_progress

        if int(time.time()) % 5 == 0:
            camera_curve_time += 1.0 / 30.0

    # Cleanup ZMQ
    socket.close()
    context.term()
    pygame.quit()

if __name__ == '__main__':
    main(parse_args())
