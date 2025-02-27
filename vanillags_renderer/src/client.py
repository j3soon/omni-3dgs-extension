import zmq
import numpy as np
import matplotlib.pyplot as plt
import simplejpeg

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
        image_data = sender.recv()
        
        if 'error' in metadata:
            print(f"Error from server: {metadata['error']}")
            return
            
        # Decode JPEG image using simplejpeg
        shape = metadata['shape']
        image = simplejpeg.decode_jpeg(
            image_data,
            colorspace='RGB'
        ) # HWC
        
        # Display the image
        plt.figure(figsize=(16, 9))
        plt.imshow(image)
        plt.show()
        
    except Exception as e:
        print(f"Error during communication: {e}")
    finally:
        sender.close()
        context.term()

if __name__ == "__main__":
    main()
