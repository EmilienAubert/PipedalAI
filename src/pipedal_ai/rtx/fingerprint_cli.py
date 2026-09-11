from __future__ import annotations

import argparse
import json
from pathlib import Path

from .fingerprints import FingerprintIndex, extract_wav_fingerprint


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Analyse locale provisoire de rendus WAV pour le classement texte."
    )
    parser.add_argument("--index", type=Path, default=Path("data/fingerprints.json"))
    commands = parser.add_subparsers(dest="command", required=True)

    analyze = commands.add_parser("analyze", help="Analyse un WAV et l'ajoute à l'index.")
    analyze.add_argument("wav", type=Path)
    analyze.add_argument("--asset-sha256", required=True)
    analyze.add_argument(
        "--source-kind",
        choices=("nam", "cab_ir", "reverb_ir", "render", "reference", "unknown"),
        default="render",
    )
    commands.add_parser("status", help="Affiche le contenu synthétique de l'index.")
    return parser


def main() -> None:
    args = _parser().parse_args()
    index = FingerprintIndex(args.index)
    if args.command == "status":
        values = index.values()
        print(json.dumps({"index": str(args.index), "count": len(values),
                          "asset_sha256": [value.asset_sha256 for value in values]}, indent=2))
        return

    wav = args.wav.expanduser().resolve(strict=True)
    if not wav.is_file() or wav.is_symlink():
        raise SystemExit("Le WAV doit être un fichier régulier et non un lien symbolique.")
    fingerprint = extract_wav_fingerprint(
        wav,
        asset_sha256=args.asset_sha256,
        source_kind=args.source_kind,
    )
    index.put(fingerprint)
    print(json.dumps(fingerprint.model_dump(mode="json"), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
