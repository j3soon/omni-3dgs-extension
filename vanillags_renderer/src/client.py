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
        'rotation': [0.0, 0.0, 0.0]
    }
    
    try:
        # Send request
        sender.send_json(test_data)
        
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
