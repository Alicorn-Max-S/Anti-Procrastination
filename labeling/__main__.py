import uvicorn

from labeling.server import HOST, PORT

uvicorn.run("labeling.server:app", host=HOST, port=PORT)
