"""
Windows Task Scheduler 등록 스크립트

실행 방법 (관리자 권한 불필요, 현재 사용자 권한으로 등록):
    py -m batch.setup_scheduler

등록 후 확인:
    schtasks /Query /TN "LH_Incheon_Batch"

수동 실행 테스트:
    schtasks /Run /TN "LH_Incheon_Batch"

등록 해제:
    schtasks /Delete /TN "LH_Incheon_Batch" /F
"""

import os
import subprocess
import sys
import textwrap

TASK_NAME = "LH_Incheon_Batch"
BATCH_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BATCH_DIR)

# 가상환경의 Python 실행 파일
PYTHON_EXE = os.path.join(PROJECT_ROOT, ".venv", "Scripts", "python.exe")
RUN_BAT = os.path.join(BATCH_DIR, "run.bat")
LOG_FILE = os.path.join(BATCH_DIR, "batch.log")


def create_run_bat():
    """Task Scheduler가 호출할 배치 파일 생성"""
    content = textwrap.dedent(f"""\
        @echo off
        cd /d "{PROJECT_ROOT}"
        "{PYTHON_EXE}" -m batch.main
    """)
    with open(RUN_BAT, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"run.bat 생성 완료: {RUN_BAT}")


def register_task():
    """Windows Task Scheduler에 일일 배치 작업 등록 (매일 09:00)"""
    cmd = [
        "schtasks", "/Create",
        "/TN", TASK_NAME,
        "/TR", f'"{RUN_BAT}"',
        "/SC", "DAILY",
        "/ST", "09:00",
        "/F",  # 이미 존재하면 덮어씀
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[오류] Task Scheduler 등록 실패:\n{result.stderr}")
        sys.exit(1)

    print(f"\nWindows Task Scheduler 등록 완료")
    print(f"  작업 이름  : {TASK_NAME}")
    print(f"  실행 시각  : 매일 오전 09:00")
    print(f"  실행 파일  : {RUN_BAT}")
    print(f"  로그 파일  : {LOG_FILE}")
    print(f"\n수동 실행 테스트: schtasks /Run /TN \"{TASK_NAME}\"")
    print(f"등록 해제      : schtasks /Delete /TN \"{TASK_NAME}\" /F")


def validate_env():
    """필수 파일 및 환경 사전 확인"""
    errors = []

    if not os.path.isfile(PYTHON_EXE):
        errors.append(f".venv Python 없음: {PYTHON_EXE}")

    main_script = os.path.join(BATCH_DIR, "main.py")
    if not os.path.isfile(main_script):
        errors.append(f"main.py 없음: {main_script}")

    env_file = os.path.join(PROJECT_ROOT, ".env")
    if not os.path.isfile(env_file):
        errors.append(f".env 파일 없음: {env_file}")

    if errors:
        for e in errors:
            print(f"[오류] {e}")
        sys.exit(1)


if __name__ == "__main__":
    validate_env()
    create_run_bat()
    register_task()
