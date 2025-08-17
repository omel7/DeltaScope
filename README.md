# DeltaScope — дифф транзакции на EVM

CLI-утилита, которая строит «дифф» по *любой* EVM-транзакции без API-ключей:
- токен-трансферы (ERC-20/721/1155),
- изменения аппрувов,
- экономика транзакции (ETH value, комиссия).

## Установка и запуск
```bash
pip install -r requirements.txt
python deltascope.py 0xTX_HASH_HERE
