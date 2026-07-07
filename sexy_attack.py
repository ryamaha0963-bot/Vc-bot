#!/usr/bin/env python3
"""
🔥 SEXY ATTACK DEPLOYMENT – TERMINAL UI 🔥
Pro Design with Colors & Animations
"""

import time
import os
import sys
from datetime import datetime

try:
    from colorama import Fore, Style, init, Back
    init(autoreset=True)
except ImportError:
    print("⚠️  Installing colorama...")
    os.system("pip install colorama")
    from colorama import Fore, Style, init, Back
    init(autoreset=True)

class SexyAttackDisplay:
    def __init__(self):
        self.target = "91.108.17.39:32000"
        self.duration = 30
        self.repos = 5
        self.runners = 10
        self.threads_per = 200
        self.total_threads = 10000
        self.strike_id = "1783426120:2e3c"
        self.launched = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.eta = (datetime.now().timestamp() + self.duration)
        self.eta_str = datetime.fromtimestamp(self.eta).strftime("%Y-%m-%d %H:%M:%S")
        self.feeds = [
            ("@sdfvhjhjg", "spider-7022edb9"),
            ("@ssvhngvc", "spider-53305543"),
            ("@wscggbh", "spider-00d68006"),
            ("@hghfbhf", "spider-558ad028")
        ]

    def draw_box(self):
        """Draw the main sexy box"""
        box = f"""
{Fore.RED}╔═══════════════════════════════════════════════════════════╗{Style.RESET_ALL}
{Fore.RED}║                                                           ║{Style.RESET_ALL}
{Fore.RED}║{Fore.YELLOW}         💀  TACTICAL NUKE INCOMING  💀                    {Fore.RED}║{Style.RESET_ALL}
{Fore.RED}║                                                           ║{Style.RESET_ALL}
{Fore.RED}╠═══════════════════════════════════════════════════════════╣{Style.RESET_ALL}
{Fore.RED}║                                                           ║{Style.RESET_ALL}
{Fore.RED}║{Fore.CYAN}  🎯 TARGET          ──  {Fore.WHITE}{self.target:<30} {Fore.RED}║{Style.RESET_ALL}
{Fore.RED}║                                                           ║{Style.RESET_ALL}
{Fore.RED}║{Fore.CYAN}  ⏱  DURATION        ──  {Fore.WHITE}{self.duration}s{' ' * 27}{Fore.RED}║{Style.RESET_ALL}
{Fore.RED}║                                                           ║{Style.RESET_ALL}
{Fore.RED}║{Fore.CYAN}  📦 REPOSITORIES    ──  {Fore.WHITE}{self.repos}/5 (MAX OUTPUT){' ' * 12}{Fore.RED}║{Style.RESET_ALL}
{Fore.RED}║                                                           ║{Style.RESET_ALL}
{Fore.RED}║{Fore.CYAN}  ⚙  RUNNERS/REPO    ──  {Fore.WHITE}{self.runners} × {self.threads_per} THREADS{' ' * 14}{Fore.RED}║{Style.RESET_ALL}
{Fore.RED}║                                                           ║{Style.RESET_ALL}
{Fore.RED}║{Fore.CYAN}  🧵 TOTAL THREADS   ──  {Fore.WHITE}{self.total_threads:,}{' ' * 27}{Fore.RED}║{Style.RESET_ALL}
{Fore.RED}║                                                           ║{Style.RESET_ALL}
{Fore.RED}║{Fore.CYAN}  🆔 STRIKE ID       ──  {Fore.WHITE}{self.strike_id:<30} {Fore.RED}║{Style.RESET_ALL}
{Fore.RED}║                                                           ║{Style.RESET_ALL}
{Fore.RED}║{Fore.CYAN}  🚀 LAUNCHED        ──  {Fore.WHITE}{self.launched}{' ' * 20}{Fore.RED}║{Style.RESET_ALL}
{Fore.RED}║                                                           ║{Style.RESET_ALL}
{Fore.RED}║{Fore.CYAN}  ⏳ ETA             ──  {Fore.WHITE}{self.eta_str}{' ' * 25}{Fore.RED}║{Style.RESET_ALL}
{Fore.RED}║                                                           ║{Style.RESET_ALL}
{Fore.RED}║{Fore.CYAN}  ⚠  THREAT LEVEL    ──  {Fore.YELLOW}███████░░░  MODERATE{Fore.RED}   ║{Style.RESET_ALL}
{Fore.RED}║                                                           ║{Style.RESET_ALL}
{Fore.RED}╠═══════════════════════════════════════════════════════════╣{Style.RESET_ALL}
{Fore.RED}║                                                           ║{Style.RESET_ALL}
{Fore.RED}║{Fore.MAGENTA}  📡 LIVE FEEDS:{Fore.RED}                                          ║{Style.RESET_ALL}
{Fore.RED}║                                                           ║{Style.RESET_ALL}"""

        # Add feeds dynamically
        for username, repo in self.feeds:
            box += f"""
{Fore.RED}║{Fore.GREEN}    ▶  {username:<10} [ {repo} ]{Fore.RED}                ║{Style.RESET_ALL}"""

        box += f"""
{Fore.RED}║                                                           ║{Style.RESET_ALL}
{Fore.RED}╚═══════════════════════════════════════════════════════════╝{Style.RESET_ALL}"""

        return box

    def animate_display(self):
        """Animate line by line"""
        os.system('clear' if os.name == 'posix' else 'cls')
        box = self.draw_box()
        
        # Print with animation
        for line in box.split('\n'):
            print(line)
            time.sleep(0.015)
        
        # Footer with glow effect
        print(f"\n{Fore.YELLOW}✦ ✦ ✦  ATTACK DEPLOYED  ✦ ✦ ✦{Style.RESET_ALL}")
        print(f"{Fore.CYAN}⚡ {self.total_threads:,} THREADS ACTIVE ⚡{Style.RESET_ALL}")
        print(f"{Fore.RED}🔥 DESTROYING TARGET 🔥{Style.RESET_ALL}\n")

    def show_progress_bar(self):
        """Show loading animation"""
        print(f"{Fore.CYAN}Initializing Attack Force...{Style.RESET_ALL}")
        for i in range(101):
            percent = i
            bar_length = 40
            filled = int(bar_length * i // 100)
            bar = '█' * filled + '░' * (bar_length - filled)
            
            # Color based on progress
            if i < 33:
                color = Fore.YELLOW
            elif i < 66:
                color = Fore.MAGENTA
            else:
                color = Fore.GREEN
            
            print(f'\r{color}▶ [{bar}] {percent}%{Style.RESET_ALL}', end='')
            time.sleep(0.03)
        print()

    def run(self):
        """Main execution"""
        print(f"{Fore.RED}🔥{Fore.YELLOW} SEXY ATTACK DEPLOYMENT {Fore.RED}🔥{Style.RESET_ALL}\n")
        self.show_progress_bar()
        time.sleep(0.5)
        self.animate_display()
        
        # Countdown
        print(f"{Fore.CYAN}⏳ COUNTDOWN TO OBLIVION{Style.RESET_ALL}")
        for i in range(5, 0, -1):
            print(f'{Fore.RED}⏰ {i}...{Style.RESET_ALL}', end=' ', flush=True)
            time.sleep(1)
        
        print(f"\n\n{Fore.GREEN}🚀 ATTACK LAUNCHED SUCCESSFULLY!{Style.RESET_ALL}")
        print(f"{Fore.RED}💀 TARGET: {self.target}{Style.RESET_ALL}")
        print(f"{Fore.YELLOW}⚡ THREADS: {self.total_threads:,}{Style.RESET_ALL}")
        print(f"{Fore.MAGENTA}⏱ DURATION: {self.duration}s{Style.RESET_ALL}\n")

# ============ RUN KARO ============
if __name__ == "__main__":
    try:
        attack = SexyAttackDisplay()
        attack.run()
    except KeyboardInterrupt:
        print(f"\n\n{Fore.RED}⚠️  ATTACK ABORTED!{Style.RESET_ALL}")
    except Exception as e:
        print(f"{Fore.RED}❌ Error: {e}{Style.RESET_ALL}")
