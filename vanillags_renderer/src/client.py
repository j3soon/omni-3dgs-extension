import zmq
import numpy as np
import matplotlib.pyplot as plt
import base64

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
        
        # Receive response
        response = sender.recv_json()
        
        if 'error' in response:
            print(f"Error from server: {response['error']}")
            return
            
        # Convert base64 string back to bytes, then to numpy array
        shape = response['shape']
        image_base64 = response['image'] # HWC
        image_bytes = base64.b64decode(image_base64)
        image = np.frombuffer(image_bytes, dtype=np.uint8).reshape(shape)
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
