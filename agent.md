---
name: Google Maps Lead Generation Agent
description: An automated agent capable of scraping business data from Google Maps based on user-provided queries and locations, then structured for export.
---

# Agent Overview
This agent is designed to bridge the gap between dynamic web-based data (Google Maps) and structured lead generation. It utilizes a Python-based scraping engine integrated with a Laravel backend to automate the discovery, extraction, and management of business information.

# Roadmap
## Phase 1: Environment & Tooling Setup
- [x] **Python Environment Configuration**: Install Python and set up a virtual environment.
- [x] **Install Scraping Dependencies**: Install Playwright and browser drivers.
- [ ] **Laravel Integration Setup**: Prepare the project structure for background execution.

## Phase 2: Core Python Scraper Development
- [x] **Browser Automation**: Implement automated navigation to Google Maps with dynamic search queries.
- [x] **Dynamic DOM Handling**: Handle infinite scrolling/lazy loading in the Maps sidebar.
- [x] **Data Extraction**: Parse and clean business entities (Name, Address, Phone, Website, Rating).
- [x] **Data Structuring**: Export results to CSV/JSON format.

## Phase 3: Laravel Backend Bridge
- [ ] **PHP Process Execution**: Integrate Laravel's `Process` component to trigger Python scripts.
- [ ] **Background Queuing**: Implement `ShouldQueue` for asynchronous scraping jobs.
- [ ] **Database Persistence**: Store scraped results via Eloquent models.

## Phase 4: Interface & Export
- [ ] **Dashboard Development**: Build input forms for scraping parameters.
- [ ] **Export Logic**: Implement CSV/XLSX export functionality using Laravel Excel.

# Technical Requirements
- **Language**: Python (Scraping engine), PHP/Laravel (Backend/Dashboard).
- **Automation**: Playwright (for dynamic content rendering).
- **Data Export**: CSV, XLSX (via Laravel Excel).
- **Communication**: PHP `Process` component for cross-language execution.
