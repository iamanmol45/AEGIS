import sys
import os

# Add api directory to path to share business logic
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))

from supervisor import AegisSupervisor


def main():
    print("[AEGIS-WORKER] Initializing autonomous background supervisor...")
    supervisor = AegisSupervisor()
    supervisor.start()


if __name__ == "__main__":
    main()
