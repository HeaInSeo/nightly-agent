#!/bin/bash
# setup-ollama.sh - Install Ollama and setup Gemma 4 Models

echo "1. Installing Ollama locally..."
curl -fsSL https://ollama.com/install.sh | sh

echo "2. Checking Ollama service status..."
if ! systemctl is-active --quiet ollama; then
    echo "Ollama is not running. Attempting to start the service..."
    sudo systemctl enable ollama
    sudo systemctl start ollama
fi

# Load model name from config.json if exists, fallback to gemma4:26b
CONFIG_FILE="config.json"
if [ -f "$CONFIG_FILE" ]; then
    MODEL_NAME=$(grep -oP '"model_name":\s*"\K[^"]+' $CONFIG_FILE || echo "gemma4:26b")
else
    MODEL_NAME="gemma4:26b"
fi

echo "3. Verifying required model: $MODEL_NAME"
if ollama list | grep -q "$MODEL_NAME"; then
    echo "✅ Model $MODEL_NAME is successfully installed and ready."
else
    echo "⚠️ Model $MODEL_NAME not found! Pulling the model now..."
    echo "(This requires ~16GB of RAM/VRAM and will take a few minutes depending on your network connection.)"
    ollama pull "$MODEL_NAME"
    if [ $? -eq 0 ]; then
        echo "✅ Model successfully downloaded."
    else
        echo "❌ Failed to pull model. Please check network or system RAM."
        exit 1
    fi
fi
