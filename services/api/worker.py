from supervisor import AegisSupervisor


def run_loop():
    """Compatibility wrapper that delegates to AegisSupervisor."""
    supervisor = AegisSupervisor()
    supervisor.start()


if __name__ == "__main__":
    run_loop()