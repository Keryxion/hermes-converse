#!/usr/bin/env python3
"""Export a committed plugin revision to a new directory."""
import argparse
import io
from pathlib import Path
import shutil
import subprocess
import tarfile


def build(root, output, *, debug=False, revision='HEAD'):
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=root)

    commit = git('rev-parse', '--verify', '--end-of-options', revision + '^{commit}').decode().strip()
    archive = git('archive', '--format=tar', commit)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        if '_diagnostic_capability.py' not in tar.getnames() or '_server_recording.py' in tar.getnames():
            raise ValueError('revision predates diagnostic artifact separation')
        implementation = git('show', f'{commit}:_server_recording.py') if debug else None
        output.mkdir(parents=True)
        try:
            tar.extractall(output, filter='data')
            if implementation is not None:
                (output / '_server_recording.py').write_bytes(implementation)
                (output / '_diagnostic_capability.py').write_text(
                    'from ._server_recording import ServerRecording, maintenance, disarm\n',
                    encoding='utf-8',
                )
        except Exception:
            shutil.rmtree(output)
            raise
    return commit


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--debug', action='store_true', help='include diagnostic recording')
    parser.add_argument('--revision', default='HEAD', help='committed revision to export')
    parser.add_argument('output', type=Path, help='new output directory')
    args = parser.parse_args()
    try:
        commit = build(Path(__file__).resolve().parent.parent, args.output,
                       debug=args.debug, revision=args.revision)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        parser.error(str(exc))
    print(f'{"debug" if args.debug else "release"} artifact at {commit}: {args.output}')


if __name__ == '__main__':
    main()
