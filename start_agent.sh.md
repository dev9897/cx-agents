#!/bin/bash
# Auto-detect Windows host IP
WINDOWS_HOST=$(ip route show | grep default | awk '{print $3}')
export OLLAMA_BASE_URL="http://${WINDOWS_HOST}:11434"
echo " Ollama URL set to: $OLLAMA_BASE_URL"

# Agent start 
source /mnt/e/AGENTIXFORCES/Python-Ag/cx-agents-main/cx-agents-main/venv/bin/activate
python main.py --mode server










PowerShell (Admin) :

powershelltaskkill /F /IM ollama.exe 2>nul
powershelltimeout /t 3 /nobreak >nul
powershell$env:OLLAMA_HOST = "0.0.0.0:11434"
powershell& "C:\Users\hp\AppData\Local\Programs\Ollama\ollama.exe" serve





powershellnetstat -ano | findstr "11434"
0.0.0.0:11434 should bee visible
Phir WSL mein agent restart karo:
bashcd /mnt/e/AGENTIXFORCES/Python-Ag/cx-agents-main/cx-agents-main && source venv/bin/activate && python main.py --mode server
Bas yahi 4-5 commands hain jo har baar chalani hain! 


http://172.23.240.1:11434/api/tags


https://ximena-excurved-sallowly.ngrok-free.dev/occ/v2/electronics/products/search?query=memory&pageSize=10&fields=FULL





pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

pip install --no-cache-dir sentence-transformers qdrant-client httpx python-dotenv
