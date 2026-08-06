import uvicorn
import sys
import os

# Statically import app.main so PyInstaller can trace dependencies
import app.main

if __name__ == "__main__":
    # Default host and port
    host = "0.0.0.0"
    port = 8000
    
    # Parse command line arguments if any
    for i, arg in enumerate(sys.argv):
        if arg == "--host" and i + 1 < len(sys.argv):
            host = sys.argv[i+1]
        elif arg == "--port" and i + 1 < len(sys.argv):
            try:
                port = int(sys.argv[i+1])
            except ValueError:
                pass
                
    # Pass the actual app object to avoid dynamic string import issues in PyInstaller
    uvicorn.run(app.main.app, host=host, port=port, reload=False)
