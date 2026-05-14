"""Tiny wrapper around tonsdk to mint fresh TON wallets for the reroller.

Each wallet is a brand-new 24-word BIP-39 mnemonic with a v4R2 contract
address on workchain 0, formatted as a user-friendly non-bounceable mainnet
address (the `UQ...` prefix that HashHeroes accounts use).
"""
from __future__ import annotations

from typing import Tuple

from tonsdk.contract.wallet import WalletVersionEnum, Wallets
from tonsdk.crypto import mnemonic_new


def new_wallet(version: str = "v4r2", workchain: int = 0) -> Tuple[list[str], str]:
    """Returns (mnemonic_words, uq_address)."""
    mnemonics = mnemonic_new()
    return wallet_from_mnemonic(mnemonics, version=version, workchain=workchain)


def wallet_from_mnemonic(
    mnemonic: list[str],
    version: str = "v4r2",
    workchain: int = 0,
) -> Tuple[list[str], str]:
    """Same shape as new_wallet() but for an existing 24-word phrase."""
    if isinstance(mnemonic, str):
        mnemonic = mnemonic.split()
    if len(mnemonic) != 24:
        raise ValueError(f"mnemonic must be 24 words, got {len(mnemonic)}")
    _, _, _, wallet = Wallets.from_mnemonics(
        mnemonic,
        WalletVersionEnum(version),
        workchain,
    )
    # to_string(is_user_friendly, is_url_safe, is_bounceable)
    addr = wallet.address.to_string(True, True, False)
    return mnemonic, addr


if __name__ == "__main__":
    mnemonic, addr = new_wallet()
    print("address:", addr)
    print("mnemonic:", " ".join(mnemonic))
