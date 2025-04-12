# Data Wizards - Hackathon 9.0

## Overview
Data Wizards is a comprehensive web scraping and analysis platform designed to extract, process, and analyze data from websites. It provides tools for dynamic scraping, keyword-based filtering, and metadata extraction, making it ideal for SEO optimization, content analysis, and more.

## Features
- **Dynamic Web Scraping**: Extract data from websites using visual selectors.
- **Keyword Filtering**: Scrape only the pages containing specific keywords.
- **Metadata Extraction**: Extract Open Graph tags, page size, load time, and more.
- **Recursive Crawling**: Crawl websites up to a specified depth.
- **Real-Time Logs**: Monitor scraping progress in real-time.
- **Browser Extension**: Easily select elements to scrape using a browser extension.

## Tech Stack
- **Frontend**: React, TypeScript, Vite, Flowbite
- **Backend**: FastAPI, MongoDB, Motor (Async MongoDB Driver)
- **Browser Extension**: Chrome Extension with JavaScript
- **Scraping Libraries**: BeautifulSoup, Requests, aiohttp

## Installation

### Prerequisites
- Python 3.9+
- Node.js 16+
- MongoDB
- Chrome Browser (for the extension)

### Backend Setup
1. Navigate to the backend directory:
   ```bash
   cd Data-Wizards/backend