import uvicorn
import sys
import os
import socket

# Statically import app.main so PyInstaller can trace dependencies
import app.main

def find_available_port(host="0.0.0.0", default_port=9000, max_attempts=50):
    """Bắt đầu từ default_port (9000). Nếu bị chiếm, tự động quét port tiếp theo (9001, 9002...)"""
    for port in range(default_port, default_port + max_attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, port))
                if port != default_port:
                    print(f" [!] Port {default_port} dang bi chiem. Tu dong chuyen sang port: {port}")
                else:
                    print(f" [*] Su dung port: {port}")
                return port
            except OSError:
                print(f" [!] Port {port} dang bi chiem, dang thu port tiep theo ({port + 1})...")
                continue
    return default_port

if __name__ == "__main__":
    # Default host and port (mặc định port 9000 theo yêu cầu)
    host = "0.0.0.0"
    target_port = 9000
    user_specified_port = False

    # Parse command line arguments if any
    for i, arg in enumerate(sys.argv):
        if arg == "--host" and i + 1 < len(sys.argv):
            host = sys.argv[i+1]
        elif arg == "--port" and i + 1 < len(sys.argv):
            try:
                target_port = int(sys.argv[i+1])
                user_specified_port = True
            except ValueError:
                pass

    if user_specified_port:
        actual_port = target_port
    else:
        actual_port = find_available_port(host, target_port)

    os.environ["GENROUTER_ACTIVE_PORT"] = str(actual_port)
    uvicorn.run(app.main.app, host=host, port=actual_port, reload=False)
