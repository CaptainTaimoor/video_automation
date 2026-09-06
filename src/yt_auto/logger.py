
import sys
from datetime import datetime
from colorama import init, Fore, Style

# Initialize colorama
init(autoreset=True)

class Logger:
    @staticmethod
    def _timestamp():
        return datetime.now().strftime("%H:%M:%S")

    @staticmethod
    def info(channel: str, message: str):
        print(f"{Fore.CYAN}[{Logger._timestamp()}] [{channel}] {message}")

    @staticmethod
    def success(channel: str, message: str):
        print(f"{Fore.GREEN}[{Logger._timestamp()}] [{channel}] SUCCESS: {message}")

    @staticmethod
    def warning(channel: str, message: str):
        print(f"{Fore.YELLOW}[{Logger._timestamp()}] [{channel}] WARNING: {message}")

    @staticmethod
    def error(channel: str, message: str, exc: Exception | None = None):
        err_msg = f"{message}"
        if exc:
            err_msg += f" | {str(exc)}"
        print(f"{Fore.RED}[{Logger._timestamp()}] [{channel}] ERROR: {err_msg}")

    @staticmethod
    def backlog_status(channel: str, count: int):
        color = Fore.YELLOW if count > 0 else Fore.GREEN
        print(f"{Fore.BLUE}[{Logger._timestamp()}] [{channel}] {Style.BRIGHT}Backlog: {color}{count} items remaining")

    @staticmethod
    def header(title: str):
        line = "=" * (len(title) + 4)
        print(f"\n{Fore.MAGENTA}{Style.BRIGHT}{line}")
        print(f"{Fore.MAGENTA}{Style.BRIGHT}| {title} |")
        print(f"{Fore.MAGENTA}{Style.BRIGHT}{line}\n")

    @staticmethod
    def subheader(title: str):
        print(f"\n{Fore.BLUE}{Style.BRIGHT}--- {title} ---")
