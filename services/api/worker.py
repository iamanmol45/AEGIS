import time
from controller import AegisController


controller = AegisController()


def run_loop():
    print("AEGIS autonomous controller started")

    while True:
        try:
            result = controller.run()
            print("AEGIS cycle:", result)

        except Exception as e:
            print("AEGIS error:", str(e))

        time.sleep(60)


if __name__ == "__main__":
    run_loop()