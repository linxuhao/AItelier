"""The ONE definition of the retained Git bundle bound.

This single number bounds every site that admits or retains a Git bundle: the
bounded read of a producer-supplied bundle file, the size of the normalized
bundle retained in State, the bundle store's ``raw_size`` SQL CHECK, and the
release batch's default maximum payload. Change it here, not at a call site:
the SQL literal and the Python gates are derived from this value.
"""
MAX_BUNDLE_BYTES = 512 * 1024 * 1024
