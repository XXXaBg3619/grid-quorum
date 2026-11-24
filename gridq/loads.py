"""Synthetic feeder loads (to be written next).

evsim's workload.py resampled Norwegian EV charging sessions and is not
ported. Plan: hospital, water pumping station, telecom base station, shelter
and residential feeders, each a daily profile built from public curves or
literature parameters plus noise, drawn with a keyed RNG per (seed, run) so the
honest and the faulty run of a pair see the same loads. Instead of claiming
one true island, sweep the shares and peak values (sensitivity analysis).
"""
