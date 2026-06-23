import sys
import asyncio
import uvicorn

def start():
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    uvicorn.run("src.control_plane.main:app", host="0.0.0.0", port=8001)

if __name__ == "__main__":
    start()
