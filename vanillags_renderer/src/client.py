import zmq
import numpy as np
import matplotlib.pyplot as plt
from PIL import Image
from io import BytesIO

def main():
    # Initialize ZMQ
    context = zmq.Context()
    sender = context.socket(zmq.REQ)
    sender.connect("ipc:///tmp/omni-3dgs-extension/vanillags_renderer")
    
    # Create test pose data with euler angles (XYZ) in radians
    test_data = {
        'position': [0.0, 0.0, 1.0],
        'rotation': [0.0, 0.0, 0.0],
    }
    
    # Create example background RGB (red) and depth (all 3.0)
    bg_rgb_np = np.ones((720, 1280, 3), dtype=np.float32) * np.array([1.0, 0.0, 0.0])
    bg_depth_np = np.full((720, 1280), 3.0, dtype=np.float32)
    
    # Convert numpy arrays to PIL Images
    bg_rgb_img = Image.fromarray((bg_rgb_np * 255).astype(np.uint8))
    bg_depth_img = Image.fromarray(bg_depth_np, mode='F')  # 'F' mode for float32
    
    # Compress as TIFF
    bg_rgb_buffer = BytesIO()
    bg_depth_buffer = BytesIO()
    bg_rgb_img.save(bg_rgb_buffer, format='TIFF')
    bg_depth_img.save(bg_depth_buffer, format='TIFF')
    
    try:
        # Send multipart message
        sender.send_json(test_data, zmq.SNDMORE)
        sender.send(bg_rgb_buffer.getvalue(), zmq.SNDMORE)
        sender.send(bg_depth_buffer.getvalue())
        
        # Receive multipart response
        metadata = sender.recv_json()
        render_data = sender.recv()
        inv_depth_data = sender.recv()
        
        if 'error' in metadata:
            print(f"Error from server: {metadata['error']}")
            return
            
        # Decode TIFF image using PIL
        shape = metadata['shape']
        render_img = Image.open(BytesIO(render_data))
        inv_depth_img = Image.open(BytesIO(inv_depth_data))
        
        # Convert to numpy arrays
        render_np = np.array(render_img)  # HWC
        inv_depth_np = np.array(inv_depth_img)  # HW
        
        # Display the image
        plt.figure(figsize=(16, 9))
        plt.subplot(1, 2, 1)
        plt.imshow(render_np)
        plt.subplot(1, 2, 2)
        plt.imshow(inv_depth_np)
        plt.show()
        
    except Exception as e:
        print(f"Error during communication: {e}")
    finally:
        sender.close()
        context.term()

if __name__ == "__main__":
    main()
