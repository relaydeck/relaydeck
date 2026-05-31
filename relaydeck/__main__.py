"""Lets `python -m relaydeck ...` work. The daemon spawner uses this so
`relaydeck daemon start` works regardless of whether the `relaydeck` console
script is on PATH inside the target interpreter's environment."""

from relaydeck import main

if __name__ == "__main__":
    main()
