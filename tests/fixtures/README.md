# Real-camera test fixtures

These are 1024 × 1024 central pixel cutouts from two different Vul8 exposures
on 2025-01-07, taken 30 seconds apart. Their original filenames are retained.
They test ingestion, archive discovery and metadata handling, not scientific
photometric accuracy. They are not for publication or AAVSO submission.

Source: the existing local `2025-01-07_Vul8_183150_Stas.rar` camera test archive
in the previous development session's scratch directory on this machine.
No new observation data were downloaded to create these fixtures.

Source archive SHA256:
`98bab6fdaf89c82aba300188dc0fcf02637823f4b1c6f11a4940becf7bf48548`.

`build_camera_fixture.py` documents and reproduces the crop. Pixel bytes are
unchanged; dimensions and any reference-pixel coordinates are adjusted;
stale FITS checksums are removed and a test-only HISTORY card is added.
Gzip timestamps and filenames are fixed for reproducibility. Compressed-file
SHA256 values are checked by the camera fixture before every use.

Tests expand copies into private pytest directories. No production image,
archive, configuration, worker, monitoring directory or web endpoint is used.
