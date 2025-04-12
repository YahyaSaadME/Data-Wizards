let networkData = new Map();
let charts = new Map();

document.getElementById('addUrlBtn').addEventListener('click', () => {
    const urlInputs = document.getElementById('urlInputs');
    const newGroup = document.createElement('div');
    newGroup.className = 'url-input-group mb-4';
    newGroup.innerHTML = `
        <div class="flex gap-2 mt-1">
            <input type="url" name="urls[]" required
                class="block w-full rounded-md border-gray-300 shadow-sm focus:border-blue-500 focus:ring-blue-500"
                placeholder="https://example.com">
            <button type="button" class="text-red-600 hover:text-red-800" onclick="this.parentElement.parentElement.remove()">×</button>
        </div>
    `;
    urlInputs.appendChild(newGroup);
});

document.getElementById('analyzeForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const urls = Array.from(document.getElementsByName('urls[]')).map(input => input.value);
    
    try {
        const response = await fetch('/analyze', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ urls })
        });
        
        const data = await response.json();
        processResults(data.results);
    } catch (error) {
        console.error('Error:', error);
        alert('An error occurred while analyzing the URLs');
    }
});

function processResults(results) {
    // Clear previous data
    networkData.clear();
    document.getElementById('analysisContent').innerHTML = '';
    
    // Get successful results
    const successResults = results.filter(r => r.status === 'success');
    
    // Update website selector with all URLs
    const selector = document.getElementById('websiteSelector');
    selector.innerHTML = '<option value="all">Show All Websites</option>' + 
        successResults.map(r => `<option value="${r.url}">${new URL(r.url).hostname}</option>`).join('');
    
    // Store results with full data including metrics
    successResults.forEach(result => {
        networkData.set(result.url, {
            data: result.data,
            page_metrics: result.page_metrics
        });
    });
    
    // Show all analyses by default
    showAllAnalyses(successResults);
    selector.value = 'all';
    
    if (successResults.length > 1) {
        displayComparison(results);
    }
}

function showAllAnalyses(results) {
    document.getElementById('analysisContent').innerHTML = '';
    results.forEach(result => createAnalysisSection(result));
}

function showAnalysis(url) {
    document.getElementById('analysisContent').innerHTML = '';
    if (url === 'all') {
        const allResults = Array.from(networkData.entries()).map(([url, data]) => ({
            url,
            data: data.data,
            page_metrics: data.page_metrics
        }));
        showAllAnalyses(allResults);
        return;
    }
    
    const storedData = networkData.get(url);
    if (!storedData) return;
    
    const result = {
        url,
        data: storedData.data,
        page_metrics: storedData.page_metrics
    };
    createAnalysisSection(result);
}

// Update website selector event listener
document.getElementById('websiteSelector').addEventListener('change', (e) => {
    const selectedUrl = e.target.value;
    if (selectedUrl) {
        showAnalysis(selectedUrl);
    } else {
        document.getElementById('analysisContent').innerHTML = '';
    }
});

function createAnalysisSection(result) {
    const template = document.getElementById('analysisTemplate');
    const clone = template.content.cloneNode(true);
    const section = clone.querySelector('.analysis-section');
    
    // Set URL
    section.querySelector('.url-display').textContent = new URL(result.url).hostname;
    
    // Update stats
    section.querySelector('.total-requests').textContent = result.data.length;
    section.querySelector('.total-size').textContent = formatSize(result.page_metrics.total_size);
    section.querySelector('.load-time').textContent = `${Math.round(result.page_metrics.total_load_time)}ms`;
    
    // Set up content type filter
    const contentTypes = new Set(result.data.map(entry => entry.content_type?.split(';')[0]).filter(Boolean));
    const contentFilter = section.querySelector('.content-filter');
    contentFilter.innerHTML = '<option value="">All</option>' + 
        Array.from(contentTypes).map(type => `<option value="${type}">${type}</option>`).join('');
    
    // Add filter listeners
    const filters = section.querySelectorAll('select, input');
    filters.forEach(filter => {
        filter.addEventListener('change', () => updateTable(result.url, section));
    });
    
    // Initial table population
    updateTable(result.url, section);
    
    document.getElementById('individualAnalysis').appendChild(section);
}

function updateTable(url, section) {
    const data = networkData.get(url);
    if (!data) return;
    
    const methodFilter = section.querySelector('.method-filter').value;
    const statusFilter = section.querySelector('.status-filter').value;
    const contentFilter = section.querySelector('.content-filter').value;
    const sizeFilter = section.querySelector('.size-filter').value;
    
    const filteredData = data.filter(entry => {
        if (methodFilter && entry.method !== methodFilter) return false;
        if (statusFilter) {
            const statusGroup = Math.floor(entry.status / 100) + 'xx';
            if (statusGroup !== statusFilter) return false;
        }
        if (contentFilter && !entry.content_type?.includes(contentFilter)) return false;
        if (sizeFilter && entry.content_size < sizeFilter * 1024) return false;
        return true;
    });
    
    const tableBody = section.querySelector('.requests-table');
    tableBody.innerHTML = filteredData.map((entry, index) => `
        <tr class="hover:bg-gray-50">
            <td class="px-6 py-4 text-sm text-gray-500">${truncateUrl(entry.url)}</td>
            <td class="px-6 py-4 text-sm text-gray-500">${entry.method}</td>
            <td class="px-6 py-4 text-sm">
                <span class="px-2 inline-flex text-xs leading-5 font-semibold rounded-full ${getStatusColor(entry.status)}">
                    ${entry.status}
                </span>
            </td>
            <td class="px-6 py-4 text-sm text-gray-500">${entry.content_type?.split(';')[0] || 'N/A'}</td>
            <td class="px-6 py-4 text-sm text-gray-500">${formatSize(entry.content_size)}</td>
            <td class="px-6 py-4 text-sm text-gray-500">${Math.round(entry.timing.total)}ms</td>
            <td class="px-6 py-4 text-sm text-gray-500">
                <button onclick="showDetails('${url}', ${index})" class="text-blue-600 hover:text-blue-800">
                    View Details
                </button>
            </td>
        </tr>
    `).join('');
    
    // Update summary stats
    section.querySelector('.total-requests').textContent = filteredData.length;
    section.querySelector('.total-size').textContent = formatSize(
        filteredData.reduce((acc, entry) => acc + entry.content_size, 0)
    );
}

function displayComparison(results) {
    document.getElementById('comparison').classList.remove('hidden');
    
    // Clear previous charts
    charts.forEach(chart => chart.destroy());
    charts.clear();
    
    const successResults = results.filter(r => r.status === 'success');
    const labels = successResults.map(r => new URL(r.url).hostname);
    
    // Load Time Chart
    charts.set('loadTime', new Chart(document.getElementById('loadTimeChart'), {
        type: 'bar',
        data: {
            labels,
            datasets: [{
                label: 'Total Load Time (ms)',
                data: successResults.map(r => r.page_metrics.total_load_time),
                backgroundColor: 'rgba(59, 130, 246, 0.5)'
            }]
        },
        options: {
            responsive: true,
            plugins: { legend: { position: 'top' } }
        }
    }));
    
    // Request Count Chart
    charts.set('requestCount', new Chart(document.getElementById('requestCountChart'), {
        type: 'bar',
        data: {
            labels,
            datasets: [{
                label: 'Number of Requests',
                data: successResults.map(r => r.data.length),
                backgroundColor: 'rgba(16, 185, 129, 0.5)'
            }]
        },
        options: {
            responsive: true,
            plugins: { legend: { position: 'top' } }
        }
    }));
    
    // Content Type Distribution
    const contentTypes = successResults.map(r => {
        const types = {};
        r.data.forEach(entry => {
            const type = entry.content_type?.split(';')[0] || 'unknown';
            types[type] = (types[type] || 0) + 1;
        });
        return types;
    });
    
    charts.set('contentType', new Chart(document.getElementById('contentTypeChart'), {
        type: 'doughnut',
        data: {
            labels: Object.keys(contentTypes[0]),
            datasets: successResults.map((r, i) => ({
                label: new URL(r.url).hostname,
                data: Object.values(contentTypes[i])
            }))
        },
        options: {
            responsive: true,
            plugins: { legend: { position: 'top' } }
        }
    }));
    
    // Status Code Distribution
    const statusCodes = successResults.map(r => {
        const codes = {};
        r.data.forEach(entry => {
            const status = Math.floor(entry.status / 100) + 'xx';
            codes[status] = (codes[status] || 0) + 1;
        });
        return codes;
    });
    
    charts.set('statusCode', new Chart(document.getElementById('statusCodeChart'), {
        type: 'bar',
        data: {
            labels: ['2xx', '3xx', '4xx', '5xx'],
            datasets: successResults.map((r, i) => ({
                label: new URL(r.url).hostname,
                data: ['2xx', '3xx', '4xx', '5xx'].map(code => statusCodes[i][code] || 0),
                backgroundColor: `hsla(${i * 360 / successResults.length}, 70%, 50%, 0.5)`
            }))
        },
        options: {
            responsive: true,
            plugins: { legend: { position: 'top' } },
            scales: { y: { beginAtZero: true } }
        }
    }));
    
    // Update comparison table
    document.getElementById('comparisonTable').innerHTML = successResults.map(r => {
        const statusCounts = { '2xx': 0, '3xx': 0, '4xx': 0, '5xx': 0 };
        r.data.forEach(entry => {
            const status = Math.floor(entry.status / 100) + 'xx';
            if (statusCounts[status] !== undefined) {
                statusCounts[status]++;
            }
        });
        
        return `
            <tr>
                <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-900">${new URL(r.url).hostname}</td>
                <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-500">${Math.round(r.page_metrics.total_load_time)}ms</td>
                <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-500">${r.data.length}</td>
                <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-500">${formatSize(r.page_metrics.total_size)}</td>
                <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-500">${statusCounts['2xx']}</td>
                <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-500">${statusCounts['3xx']}</td>
                <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-500">${statusCounts['4xx']}</td>
                <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-500">${statusCounts['5xx']}</td>
            </tr>
        `;
    }).join('');
}

function showDetails(url, index) {
    const entry = networkData.get(url)[index];
    
    // Parse URL for query parameters
    const urlObj = new URL(entry.url);
    const queryParams = Object.fromEntries(urlObj.searchParams);
    
    // Update modal content
    document.getElementById('requestHeaders').textContent = JSON.stringify(entry.request.headers, null, 2);
    document.getElementById('queryParams').textContent = JSON.stringify(queryParams, null, 2);
    document.getElementById('postData').textContent = entry.request.post_data || 'No post data';
    
    document.getElementById('responseHeaders').textContent = JSON.stringify(entry.response.headers, null, 2);
    document.getElementById('cookies').textContent = JSON.stringify(entry.response.cookies, null, 2);
    
    document.getElementById('timingConnect').textContent = `${Math.round(entry.timing.connect)}ms`;
    document.getElementById('timingWait').textContent = `${Math.round(entry.timing.wait)}ms`;
    document.getElementById('timingReceive').textContent = `${Math.round(entry.timing.receive)}ms`;
    document.getElementById('timingTotal').textContent = `${Math.round(entry.timing.total)}ms`;
    
    document.getElementById('requestModal').classList.remove('hidden');
}

function closeModal() {
    document.getElementById('requestModal').classList.add('hidden');
}

function truncateUrl(url) {
    const maxLength = 50;
    return url.length > maxLength ? url.substring(0, maxLength) + '...' : url;
}

function formatSize(bytes) {
    if (bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return `${parseFloat((bytes / Math.pow(k, i)).toFixed(2))} ${sizes[i]}`;
}

function getStatusColor(status) {
    if (status < 300) return 'bg-green-100 text-green-800';
    if (status < 400) return 'bg-blue-100 text-blue-800';
    if (status < 500) return 'bg-yellow-100 text-yellow-800';
    return 'bg-red-100 text-red-800';
}