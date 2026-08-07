"""Loader sketches for the five Impact Lab tracks.

Each sketch follows the platform contract:
- main()   — entrypoint: register_module() then run_every(interval, tick)
- sample() — returns one representative signal dict without inserting
- tick()   — one polling cycle: fetch, classify, publish
"""
