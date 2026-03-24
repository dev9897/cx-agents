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





 pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu


 pip install --no-cache-dir sentence-transformers qdrant-client httpx python-dotenv











Step #2: Digest: sha256:4ef666089da759809acccd05150295025063923d9df45c4937c38a7b66feabb8
Step #2: Status: Downloaded newer image for gcr.io/google.com/cloudsdktool/cloud-sdk:latest
Step #2: gcr.io/google.com/cloudsdktool/cloud-sdk:latest
Step #2: Applying new configuration to Cloud Run service [cx-agents-api] in project [project-5ad1588d-1a84-4bf9-9ff] region [europe-west3]
Step #2: Deploying...
Step #2: Creating Revision.................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................................done
Step #2: Routing traffic.....done
Step #2: Done.
Step #2: New configuration has been applied to service [cx-agents-api].
Step #2: URL: https://cx-agents-api-402346504896.europe-west3.run.app
Finished Step #2
PUSH
DONE
------------------------------------------------------------------------------------------------------------------------------------------------
ID                                    CREATE_TIME                DURATION  SOURCE                                                                                                        IMAGES  STATUS
da30a22a-6810-4e12-aac1-b2767e409b4c  2026-03-18T08:41:05+00:00  18M27S    gs://project-5ad1588d-1a84-4bf9-9ff_cloudbuild/source/1773823261.594265-044da6e635d94cd2b464dd25dfb8c226.tgz  -       SUCCESS
vishal@LAPTOP-DJSNITGQ:/mnt/e/AGENTIXFORCES/Python-Ag/cx-agents-main/cx-agents-main$ gcloud builds submit --config cloudbuild.yaml .