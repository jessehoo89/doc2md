# Eval Suite

This directory contains the runnable evaluation setup for ZhDocParser v0.3.0.

## What It Covers

- Generated sample PDF and DOCX parsing
- Two-column reading-order recovery
- Text-aligned table detection without visible grid lines
- Continued table merging across pages

## Run

```bash
python ./eval/run_benchmarks.py
```

The script will generate sample documents if they do not exist, run the parser against each case, compare results with reference expectations, and print a benchmark summary.
