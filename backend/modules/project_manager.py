from scraper.robots import Robots
from scraper.sitemap import Sitemap
from scraper.site import scrape_website, store_in_mongodb
from motor.motor_asyncio import AsyncIOMotorClient
from fastapi import HTTPException
import datetime
from bson import ObjectId
import traceback
import threading
import asyncio
import concurrent.futures
import queue
import json
import uuid
import time
import math
from utils.websocket_manager import ConnectionManager
import re
import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse
from pymongo import MongoClient

client = AsyncIOMotorClient("mongodb://localhost:27017")
db = client.hackathon
projects_collection = db.projects
users_collection = db.users

# Global thread pool for extraction tasks
thread_pool = concurrent.futures.ThreadPoolExecutor(max_workers=5)
# Message queue for extraction logs
message_queues = {}
# Dictionary to track active extraction processes with interrupt flags
active_extractions = {}
# Dictionary to track detailed extraction statistics
extraction_stats = {}

MAX_MONGODB_DOC_SIZE = 12 * 1024 * 1024  # 12MB document size limit
CHUNK_SIZE = 100  # Number of items per chunk

# Status constants
STATUS_RUNNING = "running"
STATUS_INTERRUPTED = "interrupted"
STATUS_COMPLETED = "completed"
STATUS_ERROR = "error"

async def add_project_with_scraping(
    user_email: str, 
    url: str, 
    ws_manager: ConnectionManager = None,
    scrape_mode: str = "limited",
    pages_limit: int = 5,
    client_id: str = None,
    search_keywords: list = None,
    include_meta: bool = True
):
    """
    Add a new project with threaded extraction, communicating progress via WebSockets.
    """
    try:
        if not url:
            raise HTTPException(status_code=400, detail="URL is required")
        
        # Validate scrape_mode
        if scrape_mode not in ["all", "limited"]:
            scrape_mode = "limited"  # Default to limited if invalid value
            
        # Validate pages_limit
        if pages_limit < 1:
            pages_limit = 5  # Set a reasonable default
        elif scrape_mode == "limited" and pages_limit > 100:
            pages_limit = 100  # Cap at 100 pages for limited mode to prevent excessive scraping
        
        # Initialize variables to track processing status
        processing_status = {
            "robots_status": "not_processed",
            "sitemap_status": "not_processed",
            "pages_found": 0,
            "pages_scraped": 0,
            "errors": [],
            "extraction_status": STATUS_RUNNING,
            "start_time": datetime.datetime.utcnow().isoformat(),
            "end_time": None,
            "scrape_mode": scrape_mode,
            "pages_limit": pages_limit,
            "search_keywords": search_keywords or [],
            "include_meta": include_meta,
            "keyword_matches": {}  # Add a place to store keyword matches
        }
        
        # Create project first to get project ID
        project_data = {
            "user_email": user_email,
            "url": url,
            "title": f"Project for {url}",
            "site_data": {
                "robots_id": None,
                "sitemap_pages": [url],  # Initialize with just the main URL
                "scraped_pages": [],
            },
            "processing_status": processing_status,
            "created_at": datetime.datetime.utcnow()
        }
        
        result = await projects_collection.insert_one(project_data)
        project_id = result.inserted_id
        
        # Update the user's projects array with the new project ID
        await users_collection.update_one(
            {"email": user_email},
            {"$push": {"projects": str(project_id)}}
        )
        
        # Create a unique client ID if not provided
        if not client_id:
            client_id = f"project_{str(project_id)}"
        
        # Register in active extractions with initial status
        active_extractions[client_id] = {
            "project_id": str(project_id),
            "status": STATUS_RUNNING,
            "interrupt_requested": False,
            "last_updated": datetime.datetime.utcnow()
        }
        
        # Initialize extraction statistics
        extraction_stats[client_id] = {
            "start_time": datetime.datetime.utcnow(),
            "robots_time": 0,
            "sitemap_time": 0,
            "scraping_time": 0,
            "pages_attempted": 0,
            "pages_successful": 0,
            "bytes_processed": 0,
            "total_elements_extracted": 0,
            "chunks_processed": 0
        }
        
        # Create a thread-specific message queue
        if ws_manager:
            message_queues[client_id] = queue.Queue()
            
            # Start message consumer in a separate task
            asyncio.create_task(consume_messages(client_id, ws_manager))
            
            # Send initial message to client
            await ws_manager.send_personal_json({
                "event": "project_created",
                "project_id": str(project_id),
                "status": "starting",
                "message": f"Project created. Starting extraction for {url}"
            }, client_id)
            
            # Start extraction in a separate thread with scrape preferences and search keywords
            thread_pool.submit(
                run_extraction_thread, 
                url, 
                str(project_id), 
                client_id, 
                user_email,
                scrape_mode,
                pages_limit,
                search_keywords,
                include_meta
            )
        
        return {
            "message": "Project added successfully, extraction started in background", 
            "project_id": str(project_id),
            "client_id": client_id,
            "processing_status": processing_status
        }
    except Exception as e:
        print(f"Error in add_project_with_scraping: {str(e)}")
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

async def consume_messages(client_id, ws_manager):
    """
    Asynchronous task to consume messages from the queue and send them via WebSocket
    """
    if client_id not in message_queues:
        print(f"No message queue found for client {client_id}")
        return
    
    q = message_queues[client_id]
    try:
        print(f"Starting message consumer for client {client_id}")
        while True:
            try:
                # Use a non-blocking get with timeout
                try:
                    message = q.get(block=False)
                    await ws_manager.send_personal_json(message, client_id)
                    q.task_done()
                except queue.Empty:
                    # No messages, wait a bit before checking again
                    await asyncio.sleep(0.1)
                    
                    # Check if client is still connected and extraction is done
                    if (client_id not in active_extractions or 
                        active_extractions[client_id]["status"] in [STATUS_COMPLETED, STATUS_ERROR, STATUS_INTERRUPTED]):
                        # If queue is empty and extraction is done, exit the loop
                        if q.empty():
                            print(f"Consumer for {client_id} exiting - extraction complete or client disconnected")
                            if client_id in message_queues:
                                del message_queues[client_id]
                            if client_id in active_extractions:
                                del active_extractions[client_id]
                            break
                    continue
                    
            except Exception as e:
                print(f"Error in message consumer for {client_id}: {str(e)}")
                print(traceback.format_exc())
                await asyncio.sleep(1)  # Prevent tight loop on error
    except Exception as e:
        print(f"Fatal error in consumer for {client_id}: {str(e)}")
        print(traceback.format_exc())
    print(f"Message consumer for {client_id} has ended")

def check_page_for_keywords(url, keywords, include_meta=True):
    """
    Check if a page contains any of the specified keywords.
    Returns (contains_keywords, matching_keywords, meta_info, contexts)
    """
    try:
        # Use a proper user agent to avoid being blocked
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/96.0.4664.110 Safari/537.36"
        }
        
        print(f"Checking URL for keywords: {url}")
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        
        # Parse the HTML
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Extract visible text content (lowercase for case-insensitive matching)
        text_content = soup.get_text().lower()
        
        # Extract meta information if requested
        meta_info = {}
        if include_meta:
            # Extract title
            title_tag = soup.find('title')
            if title_tag:
                meta_info['title'] = title_tag.get_text()
            
            # Extract meta description
            meta_desc = soup.find('meta', attrs={'name': 'description'})
            if meta_desc and meta_desc.has_attr('content'):
                meta_info['description'] = meta_desc['content']
            
            # Extract meta keywords
            meta_keywords = soup.find('meta', attrs={'name': 'keywords'})
            if meta_keywords and meta_keywords.has_attr('content'):
                meta_info['keywords'] = meta_keywords['content']
            
            # Extract OG (Open Graph) tags
            og_tags = {}
            for meta in soup.find_all('meta', attrs={'property': re.compile('^og:')}):
                if meta.has_attr('content'):
                    og_tags[meta['property']] = meta['content']
            if og_tags:
                meta_info['og'] = og_tags
        
        # Check keywords in the text content
        matching_keywords = []
        keyword_contexts = {}  # Store the context where each keyword appears
        
        for keyword in keywords:
            # Find all occurrences of the keyword in the text content
            keyword_lower = keyword.lower()
            if keyword_lower in text_content:
                matching_keywords.append(keyword)
                
                # Find context (text surrounding the keyword)
                start_index = text_content.find(keyword_lower)
                if start_index != -1:
                    context_start = max(0, start_index - 50)
                    context_end = min(len(text_content), start_index + len(keyword) + 50)
                    context = text_content[context_start:context_end]
                    keyword_contexts[keyword] = f"...{context}..."
        
        # If include_meta is True, also check in meta information
        if include_meta:
            meta_text = ' '.join([
                meta_info.get('title', ''),
                meta_info.get('description', ''),
                meta_info.get('keywords', ''),
                ' '.join([v for k, v in meta_info.get('og', {}).items()])
            ]).lower()
            
            for keyword in keywords:
                keyword_lower = keyword.lower()
                if keyword_lower in meta_text and keyword not in matching_keywords:
                    matching_keywords.append(keyword)
                    
                    # Add context info from meta tags
                    if keyword_lower in meta_info.get('title', '').lower():
                        keyword_contexts[keyword] = f"Found in title: {meta_info['title']}"
                    elif keyword_lower in meta_info.get('description', '').lower():
                        keyword_contexts[keyword] = f"Found in meta description: {meta_info['description']}"
                    elif keyword_lower in meta_info.get('keywords', '').lower():
                        keyword_contexts[keyword] = f"Found in meta keywords: {meta_info['keywords']}"
                    else:
                        keyword_contexts[keyword] = "Found in meta information"
        
        print(f"Keywords check for {url}: found {len(matching_keywords)} matches")
        return len(matching_keywords) > 0, matching_keywords, meta_info, keyword_contexts
        
    except Exception as e:
        print(f"Error checking keywords in {url}: {e}")
        print(traceback.format_exc())
        return False, [], {}, {}

def send_log(client_id, log_type, message):
    """Send a log message to the client via the message queue"""
    if client_id in message_queues:
        try:
            # Format timestamp for consistency
            timestamp = datetime.datetime.utcnow().isoformat()
            
            # Debug logging to server console to track progress
            print(f"LOG [{client_id}] [{log_type}]: {message}")
            
            # Add to message queue for websocket transmission
            message_queues[client_id].put({
                "type": log_type,
                "timestamp": timestamp,
                "message": message
            })
        except Exception as e:
            print(f"Error sending log: {e}")
    else:
        print(f"No message queue for client {client_id}, log message not sent: {message}")

def run_extraction_thread(
    url, 
    project_id, 
    client_id, 
    user_email,
    scrape_mode="limited",
    pages_limit=5,
    search_keywords=None,
    include_meta=True
):
    """
    Run the extraction process in a separate thread with keyword filtering.
    """
    print(f"Starting extraction thread for {url} with client_id {client_id}")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    # Create a thread-local MongoDB client
    thread_client = AsyncIOMotorClient("mongodb://localhost:27017")
    thread_db = thread_client.hackathon
    thread_projects_collection = thread_db.projects
    
    try:
        # Initialize processing status with scraping preferences and keywords
        processing_status = {
            "robots_status": "not_processed",
            "sitemap_status": "not_processed",
            "pages_found": 0,
            "pages_scraped": 0,
            "errors": [],
            "extraction_status": STATUS_RUNNING,
            "start_time": datetime.datetime.utcnow().isoformat(),
            "end_time": None,
            "scrape_mode": scrape_mode,
            "pages_limit": pages_limit,
            "search_keywords": search_keywords or [],
            "include_meta": include_meta
        }
        
        # Log start of extraction with search preferences
        send_log(client_id, "info", f"Starting extraction process for {url}")
        if search_keywords and len(search_keywords) > 0:
            send_log(client_id, "info", f"Using keyword filter: {', '.join(search_keywords)}")
            if include_meta:
                send_log(client_id, "info", "Including meta information in keyword search")
        
        # Step 1: Process robots.txt
        send_log(client_id, "info", f"Processing robots.txt for {url}")
        robots_start = time.time()
        robots = None
        try:
            robots = Robots(site=url)
            if hasattr(robots, '_id') and robots._id:
                processing_status["robots_status"] = "success"
                send_log(client_id, "success", f"Successfully processed robots.txt")
            else:
                processing_status["robots_status"] = "failed"
                processing_status["errors"].append("Failed to process robots.txt")
                send_log(client_id, "error", f"Failed to process robots.txt")
        except Exception as e:
            error_msg = f"Error in robots.txt processing: {str(e)}"
            processing_status["robots_status"] = "error"
            processing_status["errors"].append(error_msg)
            send_log(client_id, "error", error_msg)
        
        # Step 2: Process sitemap
        sitemap_start = time.time()
        sitemap_pages = [url]  # Default to just the main URL
        send_log(client_id, "info", f"Processing sitemap for {url}")
        
        try:
            sitemap = Sitemap(start_url=url)
            if hasattr(sitemap, 'page_urls') and sitemap.page_urls:
                sitemap_pages = list(sitemap.page_urls)
                processing_status["sitemap_status"] = "success"
                processing_status["pages_found"] = len(sitemap_pages)
                send_log(client_id, "success", f"Found {len(sitemap_pages)} pages in sitemap")
            else:
                processing_status["sitemap_status"] = "no_pages"
                processing_status["errors"].append("No pages found in sitemap")
                send_log(client_id, "warning", "No pages found in sitemap")
        except Exception as e:
            error_msg = f"Error in sitemap processing: {str(e)}"
            processing_status["sitemap_status"] = "error"
            processing_status["errors"].append(error_msg)
            send_log(client_id, "error", error_msg)
        
        # Step 3: Scrape pages - now with keyword filtering
        scraping_start = time.time()
        scraped_pages = []
        filtered_pages = []
        
        # Store keyword matches for each page
        keyword_matches = {}
        meta_info_extracted = {}
        keyword_contexts = {}
        pages_with_keywords = 0
        pages_checked = 0
        
        # First, filter the pages by keywords if keywords are provided
        if search_keywords and len(search_keywords) > 0:
            send_log(client_id, "info", f"Filtering pages by keywords: {', '.join(search_keywords)}")
            
            for i, page_url in enumerate(sitemap_pages[:pages_limit]):
                # Check for interruption
                if should_interrupt(client_id):
                    send_log(client_id, "warning", f"Filtering interrupted after checking {i} pages")
                    handle_interruption(client_id, loop, project_id, processing_status)
                    return
                
                try:
                    pages_checked += 1
                    send_log(client_id, "info", f"Checking page {pages_checked}/{len(sitemap_pages[:pages_limit])} for keywords: {page_url}")
                    
                    # Check if page contains keywords
                    contains_keywords, matches, meta_info, contexts = check_page_for_keywords(
                        page_url, 
                        search_keywords, 
                        include_meta
                    )
                    
                    if contains_keywords:
                        pages_with_keywords += 1
                        filtered_pages.append(page_url)
                        keyword_matches[page_url] = matches
                        keyword_contexts[page_url] = contexts
                        meta_info_extracted[page_url] = meta_info
                        
                        # Create detailed log message about keyword matches
                        match_details = []
                        for kw in matches:
                            context = contexts.get(kw, "No context available")
                            match_details.append(f"{kw}: {context[:100]}...")
                            
                        # Send the log message with keyword matches
                        send_log(client_id, "success", f"Page {page_url} contains keywords: {', '.join(matches)}")
                        for detail in match_details:
                            send_log(client_id, "detail", f"Match context: {detail}")
                    else:
                        # Log when no keywords are found as well
                        send_log(client_id, "warning", f"Page {page_url} does not contain any keywords, skipping")
                except Exception as e:
                    send_log(client_id, "error", f"Error checking keywords in {page_url}: {str(e)}")
                    print(f"Exception during keyword check: {str(e)}")
                    print(traceback.format_exc())
                    # Include the page anyway to avoid missing potentially important content
                    filtered_pages.append(page_url)
            
            # Add a summary of the keyword search
            if pages_with_keywords > 0:
                send_log(client_id, "info", f"Found {pages_with_keywords} pages containing keywords out of {pages_checked} checked")
            else:
                send_log(client_id, "warning", f"No pages containing the specified keywords were found after checking {pages_checked} pages")
                # Still log the search attempt
                processing_status["keyword_search_performed"] = True
                processing_status["keyword_search_results"] = "no_matches"
                processing_status["pages_checked"] = pages_checked
                processing_status["search_keywords"] = search_keywords
                
                # Update project with search information
                update_project_partial_sync(
                    thread_projects_collection, 
                    project_id, 
                    {
                        "processing_status.keyword_search_performed": True,
                        "processing_status.keyword_search_results": "no_matches",
                        "processing_status.pages_checked": pages_checked,
                        "processing_status.search_keywords": search_keywords
                    }
                )
            
            # Store keyword match information in processing status for later retrieval
            processing_status["keyword_matches"] = keyword_matches
            processing_status["keyword_contexts"] = keyword_contexts
            processing_status["pages_with_keywords"] = pages_with_keywords
            processing_status["pages_checked"] = pages_checked
            
            # Update the pages to process - if no keywords found, still process some pages
            if len(filtered_pages) > 0:
                pages_to_process = filtered_pages
                send_log(client_id, "info", "Processing only pages containing keywords")
            else:
                # If no pages match keywords, still process some pages
                pages_to_process = sitemap_pages[:min(pages_limit, len(sitemap_pages))]
                send_log(client_id, "info", "No pages matched keywords, processing a limited set of pages anyway")
        else:
            # No keywords, process all pages up to the limit
            pages_to_process = sitemap_pages[:pages_limit]
        
        send_log(client_id, "info", f"Starting to scrape {len(pages_to_process)} pages")
        
        # Now process the filtered pages
        for i, page_url in enumerate(pages_to_process):
            try:
                send_log(client_id, "info", f"Scraping page {i+1}/{len(pages_to_process)}: {page_url}")
                
                # Add keyword match information if available
                if page_url in keyword_matches:
                    send_log(client_id, "detail", f"Keyword matches: {', '.join(keyword_matches[page_url])}")
                
                # Add meta information if available
                if page_url in meta_info_extracted and meta_info_extracted[page_url]:
                    meta = meta_info_extracted[page_url]
                    if 'title' in meta:
                        send_log(client_id, "detail", f"Meta title: {meta.get('title', 'N/A')}")
                    if 'description' in meta:
                        send_log(client_id, "detail", f"Meta description: {meta.get('description', 'N/A')}")
                
                # Continue with regular scraping
                scrape_start_time = time.time()
                scraped_data = scrape_website(page_url)
                
                # If we have meta information from the keyword search, add it to scraped data
                if page_url in meta_info_extracted and meta_info_extracted[page_url]:
                    scraped_data["meta_info"] = meta_info_extracted[page_url]
                
                # Store scraped data
                store_in_mongodb(scraped_data)
                scraped_pages.append(page_url)
                
                # Log successful scraping
                send_log(client_id, "success", f"Successfully scraped {page_url}")
                
                # Log page info
                send_log(client_id, "detail", f"Page size: {scraped_data['network_metrics']['content_size_bytes'] / 1024:.1f} KB")
                if 'duration_ms' in scraped_data['network_metrics'] and scraped_data['network_metrics']['duration_ms'] > 0:
                    send_log(client_id, "detail", 
                        f"Page loaded in {scraped_data['network_metrics']['duration_ms']} ms at " +
                        f"speed: {scraped_data['network_metrics']['speed_kbps']:.1f} KB/s"
                    )
                
                # Log content stats
                text_count = len(scraped_data['content']['text_content'])
                image_count = len(scraped_data['content']['images'])
                send_log(client_id, "detail", f"Extracted {text_count + image_count} elements ({text_count} text, {image_count} images)")
                
            except Exception as e:
                error_msg = f"Error scraping {page_url}: {str(e)}"
                send_log(client_id, "error", error_msg)
                print(f"Scraping exception: {error_msg}")
                print(traceback.format_exc())
                processing_status["errors"].append(error_msg)
            
            # Check for interruption after each page
            if should_interrupt(client_id):
                send_log(client_id, "warning", f"Scraping interrupted after processing {i+1} pages")
                handle_interruption(client_id, loop, project_id, processing_status)
                return
        
        # Update processing status with final counts
        processing_status["pages_scraped"] = len(scraped_pages)
        processing_status["extraction_status"] = STATUS_COMPLETED
        processing_status["end_time"] = datetime.datetime.utcnow().isoformat()
        
        # Update project with scraped pages
        update_project_partial_sync(
            thread_projects_collection,
            project_id,
            {
                "site_data.scraped_pages": scraped_pages,
                "processing_status.pages_scraped": len(scraped_pages),
                "processing_status.extraction_status": STATUS_COMPLETED,
                "processing_status.end_time": processing_status["end_time"]
            }
        )
        
        # Final update to project with keyword match information
        final_update = {
            "processing_status.keyword_matches": keyword_matches,
            "processing_status.keyword_contexts": keyword_contexts,
            "processing_status.pages_with_keywords": pages_with_keywords,
            "processing_status.pages_checked": pages_checked,
            "processing_status.keyword_search_performed": search_keywords and len(search_keywords) > 0
        }
        
        update_project_partial_sync(
            thread_projects_collection, 
            project_id, 
            final_update
        )
        
        send_log(client_id, "info", "Extraction process completed")
        
        # Notify client of completion
        if client_id in message_queues:
            message_queues[client_id].put({
                "type": "completion",
                "timestamp": datetime.datetime.utcnow().isoformat(),
                "message": json.dumps({
                    "project_id": project_id,
                    "processing_status": {
                        "pages_found": processing_status["pages_found"],
                        "pages_scraped": processing_status["pages_scraped"]
                    }
                })
            })
    
    except Exception as e:
        error_msg = f"Unexpected error in extraction thread: {str(e)}"
        print(error_msg)
        print(traceback.format_exc())
        send_log(client_id, "error", error_msg)
        
        # Set error status
        if client_id in active_extractions:
            active_extractions[client_id]["status"] = STATUS_ERROR
            active_extractions[client_id]["last_updated"] = datetime.datetime.utcnow()
        
        # Update project with error status
        try:
            processing_status["extraction_status"] = STATUS_ERROR
            processing_status["end_time"] = datetime.datetime.utcnow().isoformat()
            processing_status["errors"].append(error_msg)
            update_project_partial_sync(
                thread_projects_collection,
                project_id, 
                {"processing_status": processing_status}
            )
        except Exception as e:
            print(f"Failed to update project with error status: {str(e)}")
    finally:
        # Make sure to close resources
        thread_client.close()
        loop.close()
        print(f"Extraction thread for client {client_id} has completed")
        
        # Clean up
        if client_id in extraction_stats:
            del extraction_stats[client_id]

def should_interrupt(client_id):
    """Check if an interruption has been requested for this client"""
    if client_id not in active_extractions:
        return False
    return active_extractions[client_id].get("interrupt_requested", False)

def handle_interruption(client_id, loop, project_id, processing_status):
    """Handle the interruption process"""
    if client_id not in active_extractions:
        return
    
    try:
        # Set status to interrupted
        active_extractions[client_id]["status"] = STATUS_INTERRUPTED
        active_extractions[client_id]["last_updated"] = datetime.datetime.utcnow()
        
        # Update processing status
        processing_status["extraction_status"] = STATUS_INTERRUPTED
        processing_status["end_time"] = datetime.datetime.utcnow().isoformat()
        
        # Get MongoDB client
        thread_client = AsyncIOMotorClient("mongodb://localhost:27017")
        thread_db = thread_client.hackathon
        thread_projects_collection = thread_db.projects
        
        # Update the project with interrupted status
        update_project_partial_sync(
            thread_projects_collection,
            project_id,
            {"processing_status": processing_status}
        )
        
        # Send log message
        send_log(client_id, "warning", "Extraction interrupted by user request")
        
        # Clean up
        thread_client.close()
        
        # Send completion message
        if client_id in message_queues:
            message_queues[client_id].put({
                "type": "completion",
                "timestamp": datetime.datetime.utcnow().isoformat(),
                "message": json.dumps({
                    "project_id": project_id,
                    "processing_status": {
                        "pages_found": processing_status.get("pages_found", 0),
                        "pages_scraped": processing_status.get("pages_scraped", 0),
                        "extraction_status": STATUS_INTERRUPTED
                    }
                })
            })
    except Exception as e:
        print(f"Error handling interruption: {str(e)}")
        print(traceback.format_exc())

def interrupt_extraction(client_id):
    """Send an interrupt signal to an extraction process"""
    if client_id not in active_extractions:
        return False
    
    active_extractions[client_id]["interrupt_requested"] = True
    active_extractions[client_id]["last_updated"] = datetime.datetime.utcnow()
    print(f"Interrupt requested for client {client_id}")
    return True

def get_extraction_status(client_id):
    """Get the current status of an extraction process"""
    if client_id not in active_extractions:
        return None
    
    status = active_extractions[client_id].copy()
    
    # Add additional stats if available
    if client_id in extraction_stats:
        status["stats"] = extraction_stats[client_id]
    
    return status

def update_project_partial_sync(collection, project_id, update_data):
    """Update a project with partial data in a synchronous way"""
    try:
        # Create a sync client to avoid asyncio issues in threads
        client = MongoClient("mongodb://localhost:27017")
        db = client.hackathon
        
        # Get the collection
        coll = db[collection.name]
        
        # Build the update document
        update_doc = {}
        for key, value in update_data.items():
            if "." in key:
                # Handle nested fields with dot notation
                update_doc[key] = value
            else:
                # Handle top-level fields
                update_doc[key] = value
        
        # Update the document
        coll.update_one({"_id": ObjectId(project_id)}, {"$set": update_doc})
        
        # Close the client
        client.close()
        
    except Exception as e:
        print(f"Error updating project: {str(e)}")
        print(traceback.format_exc())

def update_project_array_sync(collection, project_id, array_field, items):
    """Update a project array field by adding items in a synchronous way"""
    try:
        # Create a sync client to avoid asyncio issues in threads
        client = MongoClient("mongodb://localhost:27017")
        db = client.hackathon
        
        # Get the collection
        coll = db[collection.name]
        
        # Update the document by pushing to the array
        coll.update_one(
            {"_id": ObjectId(project_id)}, 
            {"$push": {array_field: {"$each": items}}}
        )
        
        # Close the client
        client.close()
        
    except Exception as e:
        print(f"Error updating project array: {str(e)}")
        print(traceback.format_exc())
