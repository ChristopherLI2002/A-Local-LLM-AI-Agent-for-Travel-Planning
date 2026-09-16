"""Upload voyage-student-1-5b-dayfill artifacts to Hugging Face Hub."""

from __future__ import annotations

from pathlib import Path

from huggingface_hub import HfApi, create_repo

REPO_ID = "ChristopherLi/voyage-student-1-5b-dayfill"
FOLDER = Path(r"D:\distill-out\hf-upload-voyage-student-1-5b-dayfill-lean")


def main() -> int:
    if not FOLDER.is_dir():
        print(f"Missing staging folder: {FOLDER}")
        return 1
    api = HfApi()
    url = create_repo(REPO_ID, repo_type="model", exist_ok=True, private=False)
    print(f"repo: {url}")
    api.upload_folder(
        folder_path=str(FOLDER),
        repo_id=REPO_ID,
        repo_type="model",
        commit_message=(
            "Add voyage-student-1-5b-dayfill (Q4_K_M GGUF + Modelfile)"
        ),
    )
    print(f"UPLOAD_OK https://huggingface.co/{REPO_ID}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
