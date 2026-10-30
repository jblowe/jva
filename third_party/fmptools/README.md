# FMP Tools (vendored)

The parts of [FMP Tools](https://github.com/evanmiller/fmptools) by Evan Miller that
`scripts/filemaker_to_csv.py` needs to read FileMaker Pro databases (fp3, fp5, fp7 and fmp12):
the library (`src/*.c`) and the `fmp2sqlite` program (`src/bin/`). MIT licence: see LICENSE.

Taken from commit fd32a6c4f425a377f27efd558cf9bc7cc09985a6 (October 2026), unchanged.
`filemaker_to_csv.py` compiles it the first time it is needed, into `build/` here (not in git),
with the system C compiler (on a Mac: the Xcode command-line tools, `xcode-select --install`).
`tests/data/governmenttypes.FP3` comes from the same project's test files.
