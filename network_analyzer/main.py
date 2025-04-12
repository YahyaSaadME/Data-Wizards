from fastapi import FastAPI, HTTPException, Request
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import WebDriverException
import json
import time
import logging
from pathlib import Path
from pydantic import BaseModel, HttpUrl

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

templates = Jinja2Templates(directory="templates")
static_dir = Path("static")
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

class URLInput(BaseModel):
    url: HttpUrl

def setup_chrome_driver():
    chrome_options = Options()
    chrome_options.add_argument('--headless')
    chrome_options.add_argument('--no-sandbox')
    chrome_options.add_argument('--disable-dev-shm-usage')
    chrome_options.set_capability('goog:loggingPrefs', {'performance': 'ALL'})
    return webdriver.Chrome(options=chrome_options)

def process_network_data(logs, start_time, end_time):
    entries = []
    request_data = {}
    
    for log in logs:
        if 'message' not in log:
            continue
            
        try:
            message = json.loads(log['message'])['message']
            
            if message['method'] == 'Network.requestWillBeSent':
                req = message['params']
                request_data[req['requestId']] = {
                    'url': req['request']['url'],
                    'method': req['request']['method'],
                    'headers': req['request']['headers'],
                    'timestamp': req['timestamp'],
                    'post_data': req['request'].get('postData')
                }
                
            elif message['method'] == 'Network.responseReceived':
                resp = message['params']
                if resp['requestId'] in request_data:
                    request = request_data[resp['requestId']]
                    response = resp['response']
                    timing = response.get('timing', {})
                    
                    # Calculate timings
                    connect_time = max(0, timing.get('connectEnd', 0) - timing.get('connectStart', 0))
                    wait_time = max(0, timing.get('receiveHeadersEnd', 0) - timing.get('sendEnd', 0))
                    receive_time = max(0, timing.get('responseEnd', 0) - timing.get('receiveHeadersEnd', 0))
                    
                    entries.append({
                        'url': request['url'],
                        'method': request['method'],
                        'status': int(response.get('status', 0)),
                        'content_type': response.get('mimeType', ''),
                        'timing': {
                            'connect': round(connect_time, 2),
                            'wait': round(wait_time, 2),
                            'receive': round(receive_time, 2),
                            'total': round(connect_time + wait_time + receive_time, 2)
                        },
                        'request': {
                            'headers': request['headers'],
                            'post_data': request['post_data']
                        },
                        'response': {
                            'headers': response.get('headers', {}),
                            'content': response.get('content', {}),
                            'cookies': response.get('cookies', [])
                        },
                        'content_size': response.get('encodedDataLength', 0)
                    })
        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f"Error processing log entry: {str(e)}")
            continue
            
    return entries

@app.get("/")
async def root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/analyze")
async def analyze_network(url_input: URLInput):
    driver = None
    try:
        driver = setup_chrome_driver()
        start_time = time.time() * 1000
        driver.get(str(url_input.url))
        
        WebDriverWait(driver, 10).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        end_time = time.time() * 1000
        
        logs = driver.get_log('performance')
        entries = process_network_data(logs, start_time, end_time)
        
        return {
            "status": "success",
            "data": entries,
            "page_metrics": {
                "total_load_time": round(end_time - start_time, 2),
                "request_count": len(entries),
                "total_size": sum(entry['content_size'] for entry in entries)
            }
        }
    except WebDriverException as e:
        logger.error(f"Selenium error: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to load the webpage")
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if driver:
            try:
                driver.quit()
            except:
                pass

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="localhost", port=8000)