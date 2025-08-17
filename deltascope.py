from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from web3 import Web3
from web3.types import LogReceipt
from hexbytes import HexBytes
from tabulate import tabulate

# --- Event signatures ---
SIG_TRANSFER = Web3.keccak(text="Transfer(address,address,uint256)").hex()
SIG_APPROVAL = Web3.keccak(text="Approval(address,address,uint256)").hex()
SIG_TF_SINGLE = Web3.keccak(text="TransferSingle(address,address,address,uint256,uint256)").hex()
SIG_TF_BATCH = Web3.keccak(text="TransferBatch(address,address,address,uint256[],uint256[])").hex()

ERC20_ABI = [
    {"constant": True, "inputs": [], "name": "symbol", "outputs": [{"name": "", "type": "string"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "type": "function"},
]

ERC721_ABI = [
    {"constant": True, "inputs": [], "name": "symbol", "outputs": [{"name": "", "type": "string"}], "type": "function"},
]

# --- Data classes ---
@dataclass
class TokenTransfer:
    token: str
    symbol: str
    standard: str  # ERC20/ERC721/ERC1155
    from_addr: str
    to_addr: str
    amount: str
    raw_amount: int
    token_id: Optional[int] = None

@dataclass
class ApprovalChange:
    token: str
    symbol: str
    owner: str
    spender: str
    amount: str
    raw_amount: int

@dataclass
class TxSummary:
    chain: str
    tx_hash: str
    block_number: int
    status: int
    from_addr: str
    to_addr: Optional[str]
    value_eth: float
    gas_used: int
    effective_gas_price_wei: int
    fee_eth: float
    transfers: List[TokenTransfer] = field(default_factory=list)
    approvals: List[ApprovalChange] = field(default_factory=list)

# --- Helpers ---
def _safe_symbol_and_decimals(w3: Web3, token_addr: str) -> Tuple[str, int, str]:
    contract = w3.eth.contract(address=token_addr, abi=ERC20_ABI)
    symbol, decimals, standard = "UNKNOWN", 18, "ERC20"
    try:
        symbol = contract.functions.symbol().call()
    except Exception:
        pass
    try:
        decimals = contract.functions.decimals().call()
    except Exception:
        # Может быть ERC721
        standard = "ERC721"
        contract721 = w3.eth.contract(address=token_addr, abi=ERC721_ABI)
        try:
            symbol = contract721.functions.symbol().call()
        except Exception:
            pass
        decimals = 0
    return symbol, decimals, standard

def _format_amount(raw: int, decimals: int) -> str:
    if decimals == 0:
        return str(raw)
    scaled = raw / (10 ** decimals)
    return f"{scaled:.8f}".rstrip("0").rstrip(".")

# --- Core parser ---
def parse_tx(w3: Web3, tx_hash: str) -> TxSummary:
    tx = w3.eth.get_transaction(tx_hash)
    receipt = w3.eth.get_transaction_receipt(tx_hash)
    chain_id = w3.eth.chain_id

    transfers, approvals = [], []

    for lg in receipt["logs"]:
        topic0 = lg["topics"][0].hex().lower()
        addr = Web3.to_checksum_address(lg["address"])
        if topic0 in (SIG_TRANSFER, SIG_TF_SINGLE, SIG_TF_BATCH, SIG_APPROVAL):
            symbol, decimals, erc_guess = _safe_symbol_and_decimals(w3, addr)

        if topic0 == SIG_TRANSFER:
            from_addr = Web3.to_checksum_address("0x" + lg["topics"][1].hex()[-40:])
            to_addr = Web3.to_checksum_address("0x" + lg["topics"][2].hex()[-40:])
            raw_amount = int.from_bytes(lg["data"], byteorder="big")
            token_id, amount_str = None, None
            if erc_guess == "ERC721" or (decimals == 0 and raw_amount < 10**10):
                token_id, amount_str, standard = raw_amount, "1", "ERC721"
            else:
                amount_str, standard = _format_amount(raw_amount, decimals), "ERC20"
            transfers.append(TokenTransfer(addr, symbol, standard, from_addr, to_addr, amount_str, raw_amount, token_id))

        elif topic0 == SIG_TF_SINGLE:
            from_addr = Web3.to_checksum_address("0x" + lg["topics"][2].hex()[-40:])
            to_addr = Web3.to_checksum_address("0x" + lg["topics"][3].hex()[-40:])
            data_bytes = HexBytes(lg["data"])
            token_id = int.from_bytes(data_bytes[:32], "big")
            raw_amount = int.from_bytes(data_bytes[32:64], "big")
            transfers.append(TokenTransfer(addr, symbol, "ERC1155", from_addr, to_addr, str(raw_amount), raw_amount, token_id))

        elif topic0 == SIG_TF_BATCH:
            from_addr = Web3.to_checksum_address("0x" + lg["topics"][2].hex()[-40:])
            to_addr = Web3.to_checksum_address("0x" + lg["topics"][3].hex()[-40:])
            transfers.append(TokenTransfer(addr, symbol, "ERC1155", from_addr, to_addr, "BATCH", 0, None))

        elif topic0 == SIG_APPROVAL:
            owner = Web3.to_checksum_address("0x" + lg["topics"][1].hex()[-40:])
            spender = Web3.to_checksum_address("0x" + lg["topics"][2].hex()[-40:])
            raw_amount = int.from_bytes(lg["data"], byteorder="big")
            amount_str = _format_amount(raw_amount, decimals)
            approvals.append(ApprovalChange(addr, symbol, owner, spender, amount_str, raw_amount))

    gas_used = receipt["gasUsed"]
    egp = receipt.get("effectiveGasPrice", 0)
    fee_eth = egp * gas_used / 1e18
    value_eth = tx["value"] / 1e18

    return TxSummary(
        chain=str(chain_id),
        tx_hash=tx_hash,
        block_number=receipt["blockNumber"],
        status=receipt["status"],
        from_addr=tx["from"],
        to_addr=tx["to"],
        value_eth=value_eth,
        gas_used=gas_used,
        effective_gas_price_wei=int(egp),
        fee_eth=fee_eth,
        transfers=transfers,
        approvals=approvals,
    )

# --- CLI ---
DEFAULT_RPC = "https://cloudflare-eth.com"

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="DeltaScope — дифф транзакции для EVM: токен-трансферы, аппрувы, комиссии.")
    ap.add_argument("tx", nargs="+", help="Tx hash(es), например: 0xabc...")
    ap.add_argument("--rpc", default=DEFAULT_RPC, help="RPC URL (по умолчанию Cloudflare)")
    ap.add_argument("--json", dest="json_path", help="Сохранить результат в JSON")
    ap.add_argument("--watch", nargs="*", default=[], help="Подсветка интересующих адресов")
    args = ap.parse_args(argv)

    w3 = Web3(Web3.HTTPProvider(args.rpc, request_kwargs={"timeout": 30}))
    if not w3.is_connected():
        raise SystemExit("Не удалось подключиться к RPC.")

    reports = []
    for txh in args.tx:
        try:
            summary = parse_tx(w3, txh)
        except Exception as e:
            print(f"[Ошибка] {txh}: {e}")
            continue

        print("=" * 80)
        print(f"Tx: {summary.tx_hash} | Chain: {summary.chain} | Block: {summary.block_number} | "
              f"Status: {'SUCCESS' if summary.status == 1 else 'FAIL'}")
        print(f"From: {summary.from_addr} -> To: {summary.to_addr}")
        print(f"ETH value: {summary.value_eth} | Fee ETH: {summary.fee_eth:.6f}")

        if summary.transfers:
            rows = []
            watch_lower = [w.lower() for w in args.watch]
            for t in summary.transfers:
                highlight = "★" if (t.from_addr.lower() in watch_lower or t.to_addr.lower() in watch_lower) else ""
                rows.append([highlight, t.standard, t.symbol, t.token, t.from_addr, t.to_addr, t.token_id or "-", t.amount])
            print("\nТокен-трансферы:")
            print(tabulate(rows, headers=["*", "Std", "Sym", "Token", "From", "To", "TokenID", "Amount"], tablefmt="github"))
        else:
            print("\nТокен-трансферы: нет")

        if summary.approvals:
            rows = []
            watch_lower = [w.lower() for w in args.watch]
            for a in summary.approvals:
                highlight = "★" if (a.owner.lower() in watch_lower or a.spender.lower() in watch_lower) else ""
                rows.append([highlight, a.symbol, a.token, a.owner, a.spender, a.amount])
            print("\nАппрувы:")
            print(tabulate(rows, headers=["*", "Sym", "Token", "Owner", "Spender", "Amount"], tablefmt="github"))
        else:
            print("\nАппрувы: нет")

        reports.append(summary.__dict__)

    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as f:
            json.dump(reports, f, ensure_ascii=False, indent=2)
        print(f"\nJSON сохранён в: {args.json_path}")

    return 0

if __name__ == "__main__":
    raise SystemExit(main())
