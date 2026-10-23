# Contributing

Bug reports and pull requests are welcome. Run the tests before you open one:

    python -m unittest discover -s tests -v

Never add firmware to this repository: no `.syx` files, no extracted
sections, no images, not even in tests. The tests build their own synthetic
files.

## Supporting another device or release: bootstrap hashes

elkflash refuses every OS file whose bootstrap is not a known stock one, and
there is no option to override that. The bootstrap runs the STARTUP menu and
the DIN MIDI recovery, so a broken one could leave a unit that cannot be
recovered without a hardware programmer. Supporting a new device or a new
Elektron release therefore means adding the hash of its stock bootstrap.

Only **stock Elektron releases** are accepted: files downloaded from
Elektron's own website, unchanged. Modified, rebuilt or third-party
bootstraps are never added, whatever they do.

### 1. Get the hash

Download the release from elektron.se and run:

    python -m elkflash bootstrap Analog-Rytm_MKII_OS1.74.syx

which prints, for example:

    file sha256: cb0f99d6...
    device:     SysEx device byte 0x0C (Analog Rytm MKII)
    container:  ELE3, 1484240 bytes
    sections:   5, 2, 1, 3, 8
    bootstrap:  section 2, 21148 bytes, sha256 9f819cdb...
    elkflash:   accepted: stock bootstrap (OS 1.73, 1.74)

If it already says `accepted`, the release shares a bootstrap that elkflash
knows and nothing needs adding.

### 2. Add it

In `elkflash/bootstrap.py`:

- **ELE3 files** (the MKII devices, the Syntakt and later): add the
  `bootstrap:` sha256 to `STOCK_BOOTSTRAPS`, under the file's SysEx device
  byte, with the release in the label. If the hash is already there for
  another release, add the version to that label instead.
- **ELE2 files** (the `container:` line says ELE2; the MKI devices): these
  have no section table, so add the `file sha256:` of the whole file to
  `STOCK_FILES`.
- **A new device**: also add its SysEx device byte and the product ids its
  Ping reply reports to `SYX_DEVICE_TO_PRODUCTS` in `elkflash/firmware.py`.
  Check on a unit: `python -m elkflash info` shows the product id and whether
  the device lists the OS upgrade commands, and
  `python -m elkflash upgrade OS.syx --dry-run` must then accept the file and
  the device without sending anything. Say in the pull request which unit you
  checked it on, and leave `--no-confirm` untested on hardware unless you are
  ready to recover over DIN (see the README's risks section).

### 3. Open a pull request

Say in the description:

- the device and the OS version;
- the exact file name and the download page on elektron.se;
- the `file sha256:` and the `bootstrap:` lines from step 1.

Do not attach the file. Whoever reviews the pull request downloads the same
release from Elektron and checks that both hashes match before merging.
