"""System One decider: Muse Glimmer answers the app's multiple-choice decisions through a local
llama-server, by reading the probabilities of the option letters (see system-one-spec.md, Part A).

Run it with `python -m decider` (FastAPI on 127.0.0.1:8090). It talks only to llama-server at
GLIMMER_URL; it never touches the app's database, Canvas or the Meta API.
"""
