import sys
import asyncio

# Set the policy BEFORE importing anything else that might initialize asyncio
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import uvicorn

def start():
    # Import app here to ensure the loop policy is already set
    from src.control_plane.main import app
    
    config = uvicorn.Config(app, host="0.0.0.0", port=8001, loop="asyncio")
    server = uvicorn.Server(config)
    asyncio.run(server.serve())

if __name__ == "__main__":
    start()

