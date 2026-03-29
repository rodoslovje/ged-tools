# GEDCOM Tools

Helper scripts for managing genealogy files in the [GEDCOM](https://en.wikipedia.org/wiki/GEDCOM) standard format.

Built with [python-gedcom](https://github.com/nickreynke/python-gedcom).

## Setup

**Requirements:** Python 3.x

```bash
# Clone the repo
git clone <repo-url>
cd ged-tools

# Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate  # on Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

## Usage

Place your `.ged` files in `data/input/` and run scripts from the `tools/` directory:

```bash
python tools/<script_name>.py data/input/file.ged
```

Output files will be written to `data/output/`.

## Project Structure

```
ged-tools/
├── tools/          # helper scripts
├── tests/          # tests
├── data/
│   ├── input/      # input .ged files (not tracked)
│   ├── output/     # output files (not tracked)
│   └── samples/    # sample .ged files
└── requirements.txt
```
