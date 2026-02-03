#!/bin/bash

# 1. 启动 Autochat 服务 (放入后台运行 &)
echo "正在启动 Autochat 服务..."
python -m src.services.autochat.serve &

# 2. 启动 查分/组卡推荐服务 (放入后台运行 &)
echo "正在启动 Deck Recommender..."
python src/services/deck_recommender/serve.py &

# 3. 启动 聊天室客户端 (已禁用)(放入后台运行 &)
echo "正在启动 Chatroom Client..."
# python -m src.services.chatroom.client &

# === 如果还有其他服务，按上面的格式继续加 ===

# 4. 休息一下
sleep 3

# 5. 最后启动 NoneBot 主程序

echo "所有子服务启动完毕，正在启动 NoneBot..."
nb run