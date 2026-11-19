"""MN calibration (WS4) and VS21 analog search (WS7): spectral versus structural similarity.

The package holds no library content. It reads reference databases built by msdial_spectrum_catalog's own
MSP parser, scores library-vs-library pairs with registered kernel identities (charter C3), joins
structural truth (Morgan-2 Tanimoto, myopic MCES, ClassyFire direct parent) and estimates, per stratum,
the score threshold at which a pair of different compounds is a structural analog with a stated
precision. See docs/mn-calibration.md.
"""

__version__ = "0.1.0"
