#!/usr/bin/env python3
"""
Convert from pickled state to an AccessoryEncoder state.

Usage:
    scripts/pickle_to_state.py accessory.pickle accessory.state

The above will read the state from the pickle file and persist it into accessory.state.
You can then pass accessory.state to the AccessoryDriver.
"""
import sys
import pickle

from pyhap.encoder import AccessoryEncoder

def convert(fromfile, tofile):
    print("Unpickling...")
    with open(fromfile, "rb") as fp:
        acc = pickle.load(fp)
    print("Persiting new state...")
    with open(tofile, "w") as fp:
        AccessoryEncoder().persist(fp, acc)
    print("Done!")

if __name__ == "__main__":
    convert(sys.argv[1], sys.argv[2])
