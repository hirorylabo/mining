import asyncio
import time
import os
import smtplib
import aiohttp
from email.message import EmailMessage
from eth_account import Account
import secrets
import logging
import multiprocessing
from typing import Optional, Tuple
from tenacity import retry, stop_after_attempt, wait_exponential
from dotenv import load_dotenv
import sys
import psutil
import websockets
import json
import signal
from collections import defaultdict
import atexit
from multiprocessing import Manager

# ロガーの設定
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
formatter = logging.Formatter(
    '%(asctime)s - %(levelname)s - %(message)s'
)
handler.setFormatter(formatter)
logger.addHandler(handler)

# グローバル変数の宣言（初期化は後で行う）
rpc_server_next_use_time = None
rpc_stats = None
session = None

class LoadBalancer:
    def __init__(self, target_cpu_percent: float = 80.0, 
                 target_memory_percent: float = 80.0,
                 check_interval: float = 5.0,
                 min_batch_size: int = 500,
                 max_batch_size: int = 5000):
        self.target_cpu_percent = target_cpu_percent
        self.target_memory_percent = target_memory_percent
        self.check_interval = check_interval
        self.min_batch_size = min_batch_size
        self.max_batch_size = max_batch_size
        self.current_batch_size = min_batch_size
        self.last_check_time = time.time()

    def adjust_batch_size(self) -> int:
        current_time = time.time()
        if current_time - self.last_check_time < self.check_interval:
            return self.current_batch_size

        cpu_usage = psutil.cpu_percent(interval=None)
        memory_usage = psutil.virtual_memory().percent

        logger.info(f"CPU Usage: {cpu_usage}%, Memory Usage: {memory_usage}%")

        # 両方の使用率が目標以下の場合、バッチサイズを増加
        if cpu_usage < self.target_cpu_percent and memory_usage < self.target_memory_percent:
            new_batch_size = min(int(self.current_batch_size * 1.2), self.max_batch_size)
            if new_batch_size != self.current_batch_size:
                logger.info(f"バッチサイズを増加: {self.current_batch_size} -> {new_batch_size}")
                self.current_batch_size = new_batch_size
        else:
            # CPUまたはメモリの使用率が目標を超えた場合、バッチサイズを減少
            new_batch_size = max(int(self.current_batch_size * 0.8), self.min_batch_size)
            if new_batch_size != self.current_batch_size:
                logger.info(f"バッチサイズを減少: {self.current_batch_size} -> {new_batch_size}")
                self.current_batch_size = new_batch_size

        self.last_check_time = current_time
        return self.current_batch_size

# .envファイルから環境変数をロード
load_dotenv()

your_email = os.getenv("EMAIL_ADDRESS")
your_password = os.getenv("EMAIL_PASSWORD")

# 送信先を固定
recipient_email = "kitano787@gmail.com"

#テストの場合 python multiv5.py --test-email

def init_shared_resources():
    """共有リソースの初期化"""
    global rpc_server_next_use_time, rpc_stats
    manager = Manager()
    rpc_server_next_use_time = manager.dict()
    rpc_stats = manager.dict({
        'success': manager.dict(),
        'error': manager.dict()
    })

def signal_handler(signum, frame):
    """シグナルハンドラー"""
    print("\nプログラムを終了します...")
    print_stats()
    sys.exit(0)

def print_stats():
    """RPCサーバーごとの統計情報を表示"""
    if rpc_stats is None:
        print("統計情報が初期化されていません")
        return

    try:
        print("\n=== RPC Server Statistics ===")
        print(f"{'Server URL':<50} {'Success':<10} {'Error':<10} {'Error Rate':<10}")
        print("-" * 80)
        
        all_servers = set()
        if rpc_stats and isinstance(rpc_stats.get('success'), dict):
            all_servers.update(rpc_stats['success'].keys())
        if rpc_stats and isinstance(rpc_stats.get('error'), dict):
            all_servers.update(rpc_stats['error'].keys())
        
        for server in all_servers:
            success = rpc_stats['success'].get(server, 0)
            error = rpc_stats['error'].get(server, 0)
            total = success + error
            error_rate = (error / total * 100) if total > 0 else 0
            print(f"{server:<50} {success:<10} {error:<10} {error_rate:.2f}%")
    except Exception as e:
        print(f"統計情報の表示中にエラーが発生しました: {e}")

async def generate_eth_address():
    # ランダムな32バイトの秘密鍵を生成
    private_key = secrets.token_hex(32)
    account = Account.from_key(private_key)
    
    # HDウォレットのニーモニックを生成（12単語）
    mnemonic = Account.create().key
    
    return (
        mnemonic.hex(),  # ニーモニック
        account.address,  # アドレス
        private_key      # 秘密鍵
    )

async def get_eth_balance(address, rpc_server_address, rpc_server_addresses):
    try:
        async with aiohttp.ClientSession() as session:
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "eth_getBalance",
                "params": [address, "latest"]
            }
            headers = {'Content-Type': 'application/json'}
            
            async with session.post(rpc_server_address, json=payload, headers=headers) as response:
                response.raise_for_status()
                result = await response.json(content_type=None)
                
                if "result" in result:
                    rpc_stats['success'][rpc_server_address] += 1
                    balance = int(result["result"], 16)
                    return balance / (10**18)
                else:
                    rpc_stats['error'][rpc_server_address] += 1
                    return 0

    except aiohttp.ClientResponseError as e:
        logger.warning(f"RPC error {e.status} with {rpc_server_address}: {e.message}")
        rpc_stats['error'][rpc_server_address] += 1
        if e.status == 403:
            logger.warning("Forbidden: 可能であれば認証情報を確認してください。")
        elif e.status == 429:
            logger.warning("Rate limit reached. Backing off.")
            await asyncio.sleep(5)
        return 0
    except aiohttp.ClientConnectionError as e:
        logger.warning(f"Could not connect to {rpc_server_address}: {e}")
        rpc_stats['error'][rpc_server_address] += 1
        return 0
    except json.JSONDecodeError as e:
        logger.error(f"Failed to decode JSON from {rpc_server_address}: {e}")
        rpc_stats['error'][rpc_server_address] += 1
        return 0
    except Exception as e:
        logger.error(f"Unexpected error with {rpc_server_address}: {e}")
        rpc_stats['error'][rpc_server_address] += 1
        return 0

async def initialize_rpc_servers(rpc_server_addresses):
    """RPCサーバーの初期化"""
    for address in rpc_server_addresses:
        if address not in rpc_server_next_use_time:
            rpc_server_next_use_time[address] = 0

async def get_transaction_count(address, rpc_server_address, rpc_server_addresses):
    global session, rpc_server_next_use_time, rpc_stats
    max_retries = len(rpc_server_addresses)
    retry_count = 0
    backoff = 1  # 初期バックオフ時間（秒）

    while retry_count < max_retries:
        current_time = time.time()
        next_use_time = rpc_server_next_use_time.get(rpc_server_address, 0)

        if current_time < next_use_time:
            retry_count += 1
            rpc_server_address = rpc_server_addresses[retry_count % len(rpc_server_addresses)]
            continue

        try:
            if session is None:
                session = aiohttp.ClientSession(
                    connector=aiohttp.TCPConnector(limit=100, ttl_dns_cache=300),
                    timeout=aiohttp.ClientTimeout(total=10)  # 10秒のタイムアウト
                )
            
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "eth_getTransactionCount",
                "params": [address, "latest"]
            }
            headers = {'Content-Type': 'application/json'}

            async with session.post(rpc_server_address, json=payload, headers=headers) as response:
                response.raise_for_status()
                result = await response.json(content_type=None)
                logger.debug(f"RPC Response from {rpc_server_address}: {result}")

                if "result" in result:
                    rpc_stats['success'][rpc_server_address] = rpc_stats['success'].get(rpc_server_address, 0) + 1
                    return int(result["result"], 16)
                
                rpc_stats['error'][rpc_server_address] = rpc_stats['error'].get(rpc_server_address, 0) + 1
                rpc_server_next_use_time[rpc_server_address] = time.time() + 30
                retry_count += 1
                rpc_server_address = rpc_server_addresses[retry_count % len(rpc_server_addresses)]
                continue

        except aiohttp.ClientResponseError as e:
            logger.warning(f"RPC error {e.status} with {rpc_server_address}: {e.message}")
            rpc_stats['error'][rpc_server_address] = rpc_stats['error'].get(rpc_server_address, 0) + 1
            rpc_server_next_use_time[rpc_server_address] = time.time() + 30
            retry_count += 1
            rpc_server_address = rpc_server_addresses[retry_count % len(rpc_server_addresses)]
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)  # 最大バックオフ時間を30秒に設定
            continue

        except aiohttp.ClientConnectionError as e:
            logger.warning(f"Could not connect to {rpc_server_address}: {e}")
            rpc_stats['error'][rpc_server_address] = rpc_stats['error'].get(rpc_server_address, 0) + 1
            rpc_server_next_use_time[rpc_server_address] = time.time() + 30
            retry_count += 1
            rpc_server_address = rpc_server_addresses[retry_count % len(rpc_server_addresses)]
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
            continue

        except json.JSONDecodeError as e:
            logger.error(f"Failed to decode JSON from {rpc_server_address}: {e}")
            rpc_stats['error'][rpc_server_address] = rpc_stats['error'].get(rpc_server_address, 0) + 1
            rpc_server_next_use_time[rpc_server_address] = time.time() + 30
            retry_count += 1
            rpc_server_address = rpc_server_addresses[retry_count % len(rpc_server_addresses)]
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
            continue

        except asyncio.TimeoutError:
            logger.warning(f"Request to {rpc_server_address} timed out.")
            rpc_stats['error'][rpc_server_address] = rpc_stats['error'].get(rpc_server_address, 0) + 1
            rpc_server_next_use_time[rpc_server_address] = time.time() + 30
            retry_count += 1
            rpc_server_address = rpc_server_addresses[retry_count % len(rpc_server_addresses)]
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
            continue

    logger.error("All RPC servers failed")
    return 0  # すべてのリトライが失敗した場合は0を返す

async def get_transaction_count_ws(address, ws_server_address, rpc_server_addresses):
    try:
        async with websockets.connect(ws_server_address) as websocket:
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "eth_getTransactionCount",
                "params": [address, "latest"]
            }
            
            await websocket.send(json.dumps(payload))
            response = await websocket.recv()
            result = json.loads(response)
            
            if "result" in result:
                count = int(result["result"], 16)
                return count
            
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        return 0

class EmailSendError(Exception):
    """メール送信に関連するエラーを表すカスタム例外クラス"""
    pass

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=4, max=10),
    reraise=True
)
async def send_email(address: str, mnemonic: str, balance: float, rpc_server_address: str) -> None:
    msg = EmailMessage()
    msg.set_content(f"Address: {address}\nMnemonic: {mnemonic}\nBalance: {balance}\nRPC Server Address: {rpc_server_address}")
    msg["Subject"] = "Ethereum address with balance > 0"
    msg["From"] = os.getenv("EMAIL_ADDRESS")
    msg["To"] = recipient_email

    try:
        with smtplib.SMTP("smtp.mail.me.com", 587) as server:
            server.starttls()
            server.login(os.getenv("EMAIL_ADDRESS"), os.getenv("EMAIL_PASSWORD"))
            server.send_message(msg)
        logger.info("Email sent successfully")
    except Exception as e:
        logger.error(f"Failed to send email: {str(e)}")
        raise EmailSendError(f"Failed to send email: {str(e)}")

async def send_report_email(total_addresses: int, elapsed_time: float) -> None:
    msg = EmailMessage()
    msg.set_content(
        f"過去1時間の実行レポート\n"
        f"チェックしたアドレス数: {total_addresses}\n"
        f"経過時間: {elapsed_time:.2f}秒"
    )
    msg["Subject"] = "Ethereum Address Generator - 1時間レポート"
    msg["From"] = os.getenv("EMAIL_ADDRESS")
    msg["To"] = recipient_email

    try:
        with smtplib.SMTP("smtp.mail.me.com", 587) as server:
            server.starttls()
            server.login(os.getenv("EMAIL_ADDRESS"), os.getenv("EMAIL_PASSWORD"))
            server.send_message(msg)
        logger.info("Report email sent successfully")
    except Exception as e:
        logger.error(f"Failed to send report email: {str(e)}")
        raise EmailSendError(f"Failed to send report email: {str(e)}")

async def main_async():
    rpc_server_addresses = [
        'https://api.zan.top/eth-mainnet',
        'wss://ethereum-rpc.publicnode.com',
        'wss://ethereum.callstaticrpc.com',
        'https://ethereum-rpc.publicnode.com',
        'https://api.securerpc.com/v1',
        'https://rpc.eth.gateway.fm',
        'https://rpc.payload.de',
        'https://eth.merkle.io',
        'https://rpc.mevblocker.io',
        'https://rpc.mevblocker.io/noreverts',
        'https://eth.meowrpc.com',
        'https://eth-mainnet.rpcfast.com?api_key=xbhWBI1Wkguk8SNMu1bvvLurPGLXmgwYeC4S6g2H7WdwFigZSmPWVZRxrskEQwIf',
        'https://rpc.lokibuilder.xyz/wallet',
        'https://rpc.mevblocker.io/fullprivacy',
        'https://eth1.lava.build',
        'https://rpc.mevblocker.io/fast',
        'https://eth.blockrazor.xyz',
        'https://rpc.flashbots.net/fast',
        'https://rpc.flashbots.net',
        'wss://mainnet.gateway.tenderly.co',
        'https://core.gashawk.io/rpc',
        'wss://eth.drpc.org',
        'https://endpoints.omniatech.io/v1/eth/mainnet/public',
        'https://eth-mainnet.nodereal.io/v1/1659dfb40aa24bbb8153a677b98064d7',
        'https://virginia.rpc.blxrbdn.com',
        'https://uk.rpc.blxrbdn.com',
        'https://singapore.rpc.blxrbdn.com',
        'https://ethereum.rpc.subquery.network/public',
        'https://mainnet.gateway.tenderly.co',
        'https://eth-mainnet.public.blastapi.io',
        'https://eth.rpc.blxrbdn.com',
        'https://ethereum.blockpi.network/v1/rpc/public',
        'https://1rpc.io/eth',
        'https://gateway.tenderly.co/public/mainnet',
        'https://eth.drpc.org',
        'https://eth-mainnet-public.unifra.io',
        'https://rpc.ankr.com/eth',
        'https://eth-pokt.nodies.app',
        'https://rpc.graffiti.farm',
        'wss://ws-rpc.graffiti.farm'
    ]
    
    load_balancer = LoadBalancer(target_cpu_percent=80.0, target_memory_percent=80.0)
    total_addresses = 0
    total_time = 0
    start_time = time.time()
    loop_counter = 0
    hourly_report_time = start_time + 3600  # 1時間後のレポート時刻を初期化
    end_time = start_time + 3600 * 24000  # 例えば5時間実行

    while time.time() < end_time:
        loop_start_time = time.time()  # 各ループの開始時間
        
        # バッチサイズを動的に調整
        num_addresses = load_balancer.adjust_batch_size()
        
        logger.info(f"Current CPU and Memory usage: CPU {psutil.cpu_percent()}%, Memory {psutil.virtual_memory().percent}% | Batch size: {num_addresses}")
        
        loop_counter += 1

        # チャンクサイズも動的に調整
        chunk_size = max(100, num_addresses // 10)
        
        # チャンク単位で非同期処理を実行
        tasks = []
        for i in range(0, num_addresses, chunk_size):
            current_chunk_size = min(chunk_size, num_addresses - i)
            chunk_tasks = [generate_eth_address() for _ in range(current_chunk_size)]
            tasks.extend(chunk_tasks)
        
        # 並列タスクの完了を待つ
        chunk_results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # エラーをフィルタリング
        successful_results = [result for result in chunk_results if not isinstance(result, Exception)]
        total_addresses += len(successful_results)

        # ループ終了時の処理時間計算
        loop_end_time = time.time()
        loop_duration = loop_end_time - loop_start_time
        total_time += loop_duration
        
        # 1秒あたりのチェック回数を計算
        current_rate = len(successful_results) / loop_duration if loop_duration > 0 else 0
        total_rate = total_addresses / total_time if total_time > 0 else 0
        
        print(f"ループ{loop_counter}回目 "
              f"処理にかかった時間: {loop_duration:.2f}秒 "
              f"チェックしたアドレス数: {total_addresses:,}個 "
              f"(今回: {len(successful_results):,}個) "
              f"1秒あたり: {current_rate:.2f}回 (累計: {total_rate:.2f}回)")

        # 1時間ごとのレポート送信
        if loop_end_time >= hourly_report_time:
            elapsed_time = loop_end_time - start_time
            try:
                await send_report_email(total_addresses, elapsed_time)
                logger.info(f"Sent hourly report. Total addresses checked: {total_addresses}")
            except Exception as e:
                logger.error(f"Error sending report email: {str(e)}")
            
            hourly_report_time = loop_end_time + 3600

    # プログラム終了時のクリーンアップ
    if session:
        await session.close()
        logger.info("aiohttpセッションを閉じました。")

def main():
    """メイン関数"""
    # 共有リソースの初期化
    init_shared_resources()
    
    # シグナルハンドラーの登録
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        print("\nプログラムが中断されました")
        print_stats()
    except Exception as e:
        print(f"予期せぬエラーが発生しました: {e}")
        print_stats()
    finally:
        print_stats()

if __name__ == '__main__':
    multiprocessing.freeze_support()
    main()