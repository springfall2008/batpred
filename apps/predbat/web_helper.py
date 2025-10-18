# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2024 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
#
# Helper functions for web pages


def get_restart_button_js():
    # Add JavaScript for restart functionality
    text = """
<script>
async function restartComponent(componentName) {
    const button = event.target;
    const originalText = button.textContent;

    // Disable button and show loading state
    button.disabled = true;
    button.textContent = 'Restarting...';

    try {
        const response = await fetch('./component_restart', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/x-www-form-urlencoded',
            },
            body: `component=${encodeURIComponent(componentName)}`
        });

        const result = await response.json();

        if (result.success) {
            button.textContent = 'Restarted!';
            button.style.backgroundColor = '#4CAF50';

            // Show success message briefly, then restore button
            setTimeout(() => {
                button.disabled = false;
                button.textContent = originalText;
                button.style.backgroundColor = '';
                // Optionally refresh the page to show updated status
                location.reload();
            }, 2000);
        } else {
            throw new Error(result.message || 'Unknown error');
        }
    } catch (error) {
        console.error('Error restarting component:', error);
        button.textContent = 'Error!';
        button.style.backgroundColor = '#f44336';

        // Restore button after showing error
        setTimeout(() => {
            button.disabled = false;
            button.textContent = originalText;
            button.style.backgroundColor = '';
        }, 2000);

        alert('Failed to restart component: ' + error.message);
    }
}
</script>
"""
    return text


def get_entity_js(entity):
    text = (
        """
        <script>
        // Entity data structure
        let allEntities = [];
        let filteredEntities = [];
        let selectedIndex = -1;
        let isDropdownVisible = false;

        // Initialize entity data

        document.addEventListener('DOMContentLoaded', function() {
            // Load entity data from API
            loadEntityData();
        });

        async function loadEntityData() {
            try {
                const response = await fetch('./api/entities');
                if (!response.ok) {
                    throw new Error('Failed to load entities');
                }
                allEntities = await response.json();

                // Set initial value if entity is selected
                const currentEntity = '"""
        + (entity.replace("'", "\\'").replace('"', '\\"') if entity else "")
        + """';
                if (currentEntity) {
                    const entityInput = document.getElementById('entitySearchInput');
                    const selectedEntity = allEntities.find(e => e.id === currentEntity);
                    if (selectedEntity) {
                        entityInput.value = selectedEntity.id;
                    }
                }

                // Set up event listeners after data is loaded
                setupEventListeners();
            } catch (error) {
                console.error('Error loading entities:', error);
                allEntities = [];
                setupEventListeners();
            }
        }

        function setupEventListeners() {
            const entityInput = document.getElementById('entitySearchInput');
            const clearButton = document.getElementById('clearEntitySearch');

            if (entityInput) {
                entityInput.addEventListener('input', filterEntityOptions);
                entityInput.addEventListener('keydown', handleEntityKeyDown);
                entityInput.addEventListener('focus', showAllEntities);
                entityInput.addEventListener('click', showAllEntities);
            }

            if (clearButton) {
                clearButton.addEventListener('click', function() {
                    entityInput.value = '';
                    document.getElementById('selectedEntityId').value = '';
                    hideEntityDropdown();
                    entityInput.focus();
                });
            }

            // Set up click outside handler
            document.addEventListener('click', function(event) {
                const container = document.querySelector('.entity-search-container');
                if (!container.contains(event.target)) {
                    hideEntityDropdown();
                }
            });
        }

        function filterEntityOptions() {
            const input = document.getElementById('entitySearchInput');
            const dropdown = document.getElementById('entityDropdown');
            const searchTerm = input.value.toLowerCase();

            // Show all entities if no search term, or filter based on search term
            if (searchTerm.length === 0) {
                filteredEntities = [...allEntities]; // Show all entities
            } else {
                // Filter entities
                filteredEntities = allEntities.filter(entity =>
                    entity.name.toLowerCase().includes(searchTerm) ||
                    entity.id.toLowerCase().includes(searchTerm)
                );
            }

            renderEntityDropdown();
        }

        function showAllEntities() {
            const input = document.getElementById('entitySearchInput');
            // If input is empty or contains a selection, show all entities
            if (input.value === '' || input.value.includes('(')) {
                filteredEntities = [...allEntities];
                renderEntityDropdown();
            }
        }

        function renderEntityDropdown() {
            const dropdown = document.getElementById('entityDropdown');

            // Group filtered entities
            const groups = {};
            filteredEntities.forEach(entity => {
                if (!groups[entity.group]) {
                    groups[entity.group] = [];
                }
                groups[entity.group].push(entity);
            });

            // Detect if we're in dark mode by checking body class or computed styles
            const isDarkMode = document.body.classList.contains('dark-mode') ||
                              getComputedStyle(document.body).backgroundColor.includes('rgb(51, 51, 51)') ||
                              getComputedStyle(document.body).color.includes('rgb(255, 255, 255)');

            // Render dropdown
            let html = '';

            Object.keys(groups).forEach(groupName => {
                html += '<div class="entity-group-header">' + groupName + '</div>';
                groups[groupName].forEach((entity, index) => {
                    const globalIndex = filteredEntities.indexOf(entity);
                    const nameColor = isDarkMode ? '#ffffff' : '#333333';
                    const idColor = isDarkMode ? '#cccccc' : '#666666';
                    html += '<div class="entity-option" data-index="' + globalIndex + '" onclick="selectEntity(\\'' + entity.id + '\\')">';
                    html += '<span class="entity-name" style="color: ' + nameColor + ' !important;">' + entity.name + '</span>';
                    html += '<span class="entity-id" style="color: ' + idColor + ' !important;">' + entity.id + '</span>';
                    html += '</div>';
                });
            });

            if (html === '' || filteredEntities.length === 0) {
                const textColor = isDarkMode ? '#ffffff' : '#333333';
                html = '<div class="entity-option" style="color: ' + textColor + ' !important;">No entities found</div>';
            }

            dropdown.innerHTML = html;
            dropdown.style.display = 'block';
            isDropdownVisible = true;
            selectedIndex = -1;
        }

        function selectEntity(entityId) {
            const entity = allEntities.find(e => e.id === entityId);
            if (entity) {
                const input = document.getElementById('entitySearchInput');
                const hiddenInput = document.getElementById('selectedEntityId');

                input.value = entity.id;
                hiddenInput.value = entity.id;

                hideEntityDropdown();

                // Submit the form
                document.getElementById('entitySelectForm').submit();
            }
        }

        function hideEntityDropdown() {
            const dropdown = document.getElementById('entityDropdown');
            dropdown.style.display = 'none';
            isDropdownVisible = false;
            selectedIndex = -1;
        }

        function handleEntityKeyDown(event) {
            if (!isDropdownVisible) return;

            const options = document.querySelectorAll('.entity-option[data-index]');

            if (event.key === 'ArrowDown') {
                event.preventDefault();
                selectedIndex = Math.min(selectedIndex + 1, options.length - 1);
                updateSelection(options);
            } else if (event.key === 'ArrowUp') {
                event.preventDefault();
                selectedIndex = Math.max(selectedIndex - 1, -1);
                updateSelection(options);
            } else if (event.key === 'Enter') {
                event.preventDefault();
                if (selectedIndex >= 0 && options[selectedIndex]) {
                    const entityIndex = parseInt(options[selectedIndex].getAttribute('data-index'));
                    const entity = filteredEntities[entityIndex];
                    if (entity) {
                        selectEntity(entity.id);
                    }
                }
            } else if (event.key === 'Escape') {
                event.preventDefault();
                hideEntityDropdown();
            }
        }

        function updateSelection(options) {
            options.forEach((option, index) => {
                option.classList.toggle('selected', index === selectedIndex);
            });

            if (selectedIndex >= 0 && options[selectedIndex]) {
                options[selectedIndex].scrollIntoView({ block: 'nearest' });
            }
        }
        </script>
    """
    )
    return text


def get_entity_css():
    html = """
        <style>
        .entity-search-container {
            position: relative;
        }
        .entity-dropdown {
            position: absolute;
            top: 100%;
            left: 0;
            right: 0;
            min-width: 500px;
            background: white;
            border: 1px solid #ddd;
            border-top: none;
            max-height: 300px;
            overflow-y: auto;
            z-index: 1000;
            display: none;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        .entity-option {
            padding: 10px 15px;
            cursor: pointer;
            border-bottom: 1px solid #eee;
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 10px;
        }
        .entity-option:hover,
        .entity-option.selected {
            background-color: #f0f0f0;
        }
        .entity-option .entity-name {
            font-weight: bold;
            flex: 0 1 40%;
            margin-right: 15px;
            font-size: 14px;
            word-break: break-word;
            overflow: hidden;
            text-overflow: ellipsis;
            color: #333;
        }
        .entity-option .entity-id {
            color: #666;
            font-size: 11px;
            flex: 0 1 60%;
            text-align: right;
            font-family: monospace;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .entity-group-header {
            padding: 8px 10px;
            background-color: #f5f5f5;
            font-weight: bold;
            color: #333;
            border-bottom: 1px solid #ddd;
            font-size: 12px;
        }
        /* Dark mode styles */
        body.dark-mode .entity-dropdown {
            background: #333 !important;
            border: 1px solid #666 !important;
            color: #fff !important;
        }
        body.dark-mode .entity-dropdown * {
            color: #fff !important;
        }
        body.dark-mode .entity-option {
            border-bottom: 1px solid #555 !important;
            color: #fff !important;
        }
        body.dark-mode .entity-option:hover,
        body.dark-mode .entity-option.selected {
            background-color: #555;
        }
        body.dark-mode .entity-dropdown .entity-option .entity-name {
            color: #fff !important;
        }
        body.dark-mode .entity-option .entity-name {
            color: #fff !important;
        }
        body.dark-mode .entity-option .entity-id {
            color: #ccc !important;
        }
        body.dark-mode .entity-group-header {
            background-color: #444;
            color: #e0e0e0;
            border-bottom: 1px solid #666;
        }
        body.dark-mode #entitySearchInput {
            background-color: #333;
            color: #fff;
            border: 1px solid #666;
        }
        body.dark-mode #clearEntitySearch {
            color: #ccc !important;
        }
        body.dark-mode #clearEntitySearch:hover {
            color: #fff !important;
        }
        </style>
"""
    return html


def get_entity_control_css():
    # Add CSS for dark mode support
    html = """
    <style>
    .entity-edit-container {
        --border-color: #ddd;
        --background-secondary: #f9f9f9;
        --text-color: #333;
        --text-secondary: #666;
        --input-background: #fff;
    }

    body.dark-mode .entity-edit-container {
        --border-color: #555;
        --background-secondary: #2d2d2d;
        --text-color: #e0e0e0;
        --text-secondary: #bbb;
        --input-background: #3d3d3d;
        color: var(--text-color);
    }

    body.dark-mode .entity-edit-container input[type="number"],
    body.dark-mode .entity-edit-container select {
        background-color: var(--input-background) !important;
        border-color: var(--border-color) !important;
        color: var(--text-color) !important;
    }

    body.dark-mode .entity-edit-container span {
        color: var(--text-color) !important;
    }
    </style>
    """
    return html


def get_entity_toggle_js():
    """
    JavaScript for entity toggle buttons
    """
    html = f"""
    <script>
    function toggleEntitySwitch(button, entityId, days) {{
        // Toggle the visual state
        button.classList.toggle('active');

        // Determine the new value
        const newValue = button.classList.contains('active') ? 'on' : 'off';

        // Create form data
        const formData = new FormData();
        formData.append('entity_id', entityId);
        formData.append('days', days);
        formData.append('value', newValue);

        // Submit the form
        fetch('./entity', {{
            method: 'POST',
            body: formData
        }}).then(response => {{
            if (response.redirected) {{
                window.location.href = response.url;
            }}
        }}).catch(error => {{
            console.error('Error:', error);
            // Revert the toggle on error
            button.classList.toggle('active');
        }});
    }}
    </script>
    """
    return html


def get_apps_js(all_states_json):
    text = (
        f"""
<script>
// Global object to track pending changes
let pendingChanges = {{}};

// All Home Assistant states for entity dropdown
const allStates = """
        + all_states_json
        + """;
function showMessage(message, type) {
    const container = document.getElementById('messageContainer');
    container.className = 'message-container message-' + type;
    container.textContent = message;
    container.style.display = 'block';

    // Auto-hide success messages after 5 seconds
    if (type === 'success') {
        setTimeout(() => {
            container.style.display = 'none';
        }, 5000);
    }
}

function updateChangeCounter() {
    const changeCount = Object.keys(pendingChanges).length;
    const changeCountElement = document.getElementById('changeCount');
    const saveButton = document.getElementById('saveAllButton');
    const discardButton = document.getElementById('discardAllButton');

    if (changeCount === 0) {
        changeCountElement.textContent = 'No unsaved changes';
        saveButton.disabled = true;
        discardButton.disabled = true;
    } else {
        changeCountElement.textContent = `${changeCount} unsaved change${changeCount > 1 ? 's' : ''}`;
        saveButton.disabled = false;
        discardButton.disabled = false;
    }
}

function markRowAsChanged(rowId) {
    const row = document.getElementById('row_' + rowId);
    row.classList.add('row-changed');
}

function unmarkRowAsChanged(rowId) {
    const row = document.getElementById('row_' + rowId);
    row.classList.remove('row-changed');
}

function toggleValue(rowId) {
    const row = document.getElementById('row_' + rowId);
    const argName = row.dataset.argName;
    const toggleButton = row.querySelector('.toggle-button');
    const currentValue = toggleButton.dataset.value === 'true';
    const newValue = !currentValue;

    // Track the change locally
    pendingChanges[argName] = {
        rowId: rowId,
        originalValue: row.dataset.originalValue,
        newValue: newValue.toString(),
        type: 'boolean'
    };

    // Update the toggle button state visually
    toggleButton.dataset.value = newValue.toString();
    if (newValue) {
        toggleButton.classList.add('active');
    } else {
        toggleButton.classList.remove('active');
    }

    // Update the value display
    const valueCell = document.getElementById('value_' + rowId);
    valueCell.innerHTML = newValue.toString();

    // Mark row as changed and update counter
    markRowAsChanged(rowId);
    updateChangeCounter();
}

function editValue(rowId) {
    const row = document.getElementById('row_' + rowId);
    const valueCell = document.getElementById('value_' + rowId);
    const argName = row.dataset.argName;
    const originalValue = row.dataset.originalValue;

    // Check if there's a pending change, use that value instead of original
    const currentValue = pendingChanges[argName] ? pendingChanges[argName].newValue : originalValue;

    // Check if this is an entity string (contains dots)
    if (currentValue && currentValue.match(/^[a-zA-Z]+\\.\\S+/)) {
        // Show entity dropdown
        showEntityDropdown(rowId, currentValue);
    } else {
        // Show regular text input for non-entity values
        valueCell.innerHTML = `
            <input type="text" class="edit-input" id="input_${rowId}" value="${currentValue}">
            <button class="save-button" onclick="saveValue(${rowId})">Apply</button>
            <button class="cancel-button" onclick="cancelEdit(${rowId})">Cancel</button>
        `;

        // Focus the input field
        document.getElementById('input_' + rowId).focus();
    }
}

function getDisplayValueEntity(entityId) {
    // Get the state of the entity from allStates
    if (typeIsEntity(entityId) && allStates[entityId])
    {
        const entityState = allStates[entityId];
        const state = entityState.state || '';
        const unit = entityState.unit_of_measurement || '';
        return `${entityId} = ${state} ${unit}`;
    }
    return entityId; // Fallback to just the entity ID if no state found
}

function cancelEdit(rowId) {
    const row = document.getElementById('row_' + rowId);
    const valueCell = document.getElementById('value_' + rowId);
    const argName = row.dataset.argName;

    // Check if there's a pending change for this row
    if (pendingChanges[argName]) {
        // Show the pending value - need to check if it's an entity
        const pendingValue = pendingChanges[argName].newValue;
        valueCell.innerHTML = getDisplayValueEntity(pendingValue);
    } else {
        // Show the original value - need to check if it's an entity
        const originalValue = row.dataset.originalValue;
        valueCell.innerHTML = getDisplayValueEntity(originalValue);
    }
}

function typeIsEntity(value) {
    if (value.match(/^[a-zA-Z]+\\.\\S+/) && !typeIsNumerical(value))
    {
        return true; // This looks like an entity ID (contains dots but is not a number)
    }
    return false; // Not an entity ID
}

function typeIsNumerical(value) {
    // Check if the string value can be number (integer or float)

    // Check if the value is a valid number
    if (!/^-?\\d*\\.?\\d+$/.test(value)) {
        return false; // Not a valid numerical format
    }

    try {
        if (value.includes('.')) {
            value = parseFloat(value);
            console.log("Parsed as float:", value);
        } else {
            value = parseInt(value);
            console.log("Parsed as integer:", value);
        }
    } catch (e) {
        return false; // Not a numerical value
    }
    return !isNaN(value); // Check if it's a valid number
}

function determineValueType(value) {
    let valueType = 'string';
    if (typeIsEntity(value)) {
        valueType = 'entity';
    } else if (typeIsNumerical(value)) {
        valueType = 'numerical';
    }
    return valueType;
}

function saveValue(rowId) {
    const row = document.getElementById('row_' + rowId);
    const input = document.getElementById('input_' + rowId);
    const argName = row.dataset.argName;
    const newValue = input.value.trim();
    const originalValue = row.dataset.originalValue;

    // Validate the input
    if (newValue === '') {
        showMessage('Value cannot be empty', 'error');
        return;
    }

    // Determine if this is an entity or numerical value
    let valueType = determineValueType(originalValue);
    if (valueType === 'numerical' && newValue !== originalValue) {
        if (!typeIsNumerical(newValue)) {
            showMessage('Invalid number format', 'error');
            return;
        }
    }

    // Track the change locally
    if (newValue !== originalValue) {
        pendingChanges[argName] = {
            rowId: rowId,
            originalValue: originalValue,
            newValue: newValue,
            type: valueType
        };
        markRowAsChanged(rowId);
    } else {
        // If value is same as original, remove from pending changes
        if (pendingChanges[argName]) {
            delete pendingChanges[argName];
            unmarkRowAsChanged(rowId);
        }
    }

    // Update the display value
    const valueCell = document.getElementById('value_' + rowId);
    valueCell.innerHTML = getDisplayValueEntity(newValue);

    updateChangeCounter();
}

// Entity dropdown functions for editing entity strings
function showEntityDropdown(rowId, currentValue) {
    const valueCell = document.getElementById('value_' + rowId);

    // Create the dropdown container
    valueCell.innerHTML = `
        <div class="entity-dropdown-container">
            <input type="text" class="entity-search-input" id="entity_search_${rowId}"
                   placeholder="Type to filter entities..." autocomplete="off">
            <div class="entity-dropdown" id="entity_dropdown_${rowId}"></div>
            <div style="margin-top: 5px;">
                <button class="save-button" onclick="saveEntityValue(${rowId})">Apply</button>
                <button class="cancel-button" onclick="cancelEdit(${rowId})">Cancel</button>
            </div>
        </div>
    `;

    // Set up search functionality first
    setupEntitySearch(rowId, currentValue);

    // If current value is an entity, populate with it as initial filter
    if (currentValue && typeIsEntity(currentValue)) {
        populateEntityDropdown(rowId, currentValue, currentValue);
    } else {
        populateEntityDropdown(rowId, currentValue);
    }

    // Focus the search input and select all text for easy replacement
    const searchInput = document.getElementById('entity_search_' + rowId);
    searchInput.focus();
    searchInput.select();
}

function populateEntityDropdown(rowId, currentValue, filterText = '') {
    const dropdown = document.getElementById('entity_dropdown_' + rowId);
    const searchInput = document.getElementById('entity_search_' + rowId);

    if (!dropdown) return;

    dropdown.innerHTML = '';

    // Get all entity IDs and filter them
    const entities = Object.keys(allStates);
    const filteredEntities = entities.filter(entityId =>
        entityId.toLowerCase().includes(filterText.toLowerCase())
    ).sort();

    // Limit results to prevent performance issues
    const maxResults = 100;
    const limitedEntities = filteredEntities.slice(0, maxResults);

    if (limitedEntities.length === 0) {
        dropdown.innerHTML = '<div class="entity-option">No entities found</div>';
        return;
    }

    limitedEntities.forEach(entityId => {
        const entityState = allStates[entityId];
        const entityValue = entityState && entityState.state ? entityState.state : 'unknown';
        const unit_of_measurement = entityState && entityState.unit_of_measurement ? entityState.unit_of_measurement : '';

        const option = document.createElement('div');
        option.className = 'entity-option';
        option.dataset.entityId = entityId;

        // Highlight current selection (only if search input is empty or matches exactly)
        if (entityId === currentValue && (!filterText || filterText === entityId)) {
            option.classList.add('selected');
        }

        option.innerHTML = `
            <div class="entity-name">${entityId}</div>
            <div class="entity-value">${entityValue}${unit_of_measurement}</div>
        `;

        option.addEventListener('click', () => {
            // Clear previous selections
            dropdown.querySelectorAll('.entity-option').forEach(opt => opt.classList.remove('selected'));
            // Select this option
            option.classList.add('selected');
            searchInput.value = entityId;
        });

        dropdown.appendChild(option);
    });

    if (filteredEntities.length > maxResults) {
        const moreOption = document.createElement('div');
        moreOption.className = 'entity-option';
        moreOption.style.fontStyle = 'italic';
        moreOption.innerHTML = `<div class="entity-name">... and ${filteredEntities.length - maxResults} more (refine search)</div>`;
        dropdown.appendChild(moreOption);
    }
}

function setupEntitySearch(rowId, currentValue) {
    const searchInput = document.getElementById('entity_search_' + rowId);

    if (!searchInput) return;

    // Set initial value if it's an entity
    if (currentValue && currentValue.includes('.')) {
        searchInput.value = currentValue;
    }

    // Handle search input
    searchInput.addEventListener('input', (e) => {
        const filterText = e.target.value;
        populateEntityDropdown(rowId, currentValue, filterText);
    });

    // Handle keyboard navigation
    searchInput.addEventListener('keydown', (e) => {
        const dropdown = document.getElementById('entity_dropdown_' + rowId);
        const options = dropdown.querySelectorAll('.entity-option[data-entity-id]');
        const selected = dropdown.querySelector('.entity-option.selected');

        let newSelection = null;

        if (e.key === 'ArrowDown') {
            e.preventDefault();
            if (selected) {
                newSelection = selected.nextElementSibling;
                if (!newSelection || !newSelection.dataset.entityId) {
                    newSelection = options[0];
                }
            } else {
                newSelection = options[0];
            }
        } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            if (selected) {
                newSelection = selected.previousElementSibling;
                if (!newSelection || !newSelection.dataset.entityId) {
                    newSelection = options[options.length - 1];
                }
            } else {
                newSelection = options[options.length - 1];
            }
        } else if (e.key === 'Enter') {
            e.preventDefault();
            if (selected && selected.dataset.entityId) {
                searchInput.value = selected.dataset.entityId;
            }
            saveEntityValue(rowId);
            return;
        } else if (e.key === 'Escape') {
            e.preventDefault();
            cancelEdit(rowId);
            return;
        }

        if (newSelection) {
            // Clear previous selections
            options.forEach(opt => opt.classList.remove('selected'));
            // Select new option
            newSelection.classList.add('selected');
            // Only update input value on Enter, not on arrow key navigation
            // searchInput.value = newSelection.dataset.entityId;
            // Scroll into view if needed
            newSelection.scrollIntoView({ block: 'nearest' });
        }
    });
}

function saveEntityValue(rowId) {
    const row = document.getElementById('row_' + rowId);
    const searchInput = document.getElementById('entity_search_' + rowId);
    const argName = row.dataset.argName;
    const newValue = searchInput.value.trim();
    const originalValue = row.dataset.originalValue;

    // Validate the input
    if (newValue === '') {
        showMessage('Entity value cannot be empty', 'error');
        return;
    }

    // Validate that it's a valid entity ID (contains at least one dot)
    if (!typeIsEntity(newValue)) {
        showMessage('Please select a valid entity ID', 'error');
        return;
    }

    // New value without text after dollar (if existing)
    const newValueBase = newValue.split('$')[0].trim();

    // Check if entity exists in allStates
    if (!allStates[newValueBase]) {
        if (!confirm(`Entity "${newValueBase}" was not found in Home Assistant. Do you want to use it anyway?`)) {
            return;
        }
    }

    // Track the change locally
    if (newValue !== originalValue) {
        pendingChanges[argName] = {
            rowId: rowId,
            originalValue: originalValue,
            newValue: newValue,
            type: 'entity'
        }
        markRowAsChanged(rowId);
    } else {
        // If value is same as original, remove from pending changes
        if (pendingChanges[argName]) {
            delete pendingChanges[argName];
            unmarkRowAsChanged(rowId);
        }
    }

    // Update the display value - show entity's current state value, not just the entity ID
    const valueCell = document.getElementById('value_' + rowId);

    // Get the entity's current state value for display
    valueCell.innerHTML = getDisplayValueEntity(newValue);
    updateChangeCounter();
}

// Nested entity dropdown functions for editing entity strings within lists/dictionaries
function showNestedEntityDropdown(rowId, currentValue) {
    const valueCell = document.getElementById('nested_value_' + rowId);

    // Create the dropdown container
    valueCell.innerHTML = `
        : <div class="entity-dropdown-container" style="min-width: 450px;">
            <input type="text" class="entity-search-input" id="nested_entity_search_${rowId}"
                   placeholder="Type to filter entities..." autocomplete="off" style="min-width: 400px;">
            <div class="entity-dropdown" id="nested_entity_dropdown_${rowId}" style="min-width: 400px;"></div>
            <div style="margin-top: 5px;">
                <button class="save-button" onclick="saveNestedEntityValue(${rowId})">Apply</button>
                <button class="cancel-button" onclick="cancelNestedEdit(${rowId})">Cancel</button>
            </div>
        </div>
    `;

    // Set up search functionality first
    setupNestedEntitySearch(rowId, currentValue);

    // If current value is an entity, populate with it as initial filter
    if (currentValue && currentValue.includes('.')) {
        populateNestedEntityDropdown(rowId, currentValue, currentValue);
    } else {
        populateNestedEntityDropdown(rowId, currentValue);
    }

    // Focus the search input and select all text for easy replacement
    const searchInput = document.getElementById('nested_entity_search_' + rowId);
    searchInput.focus();
    searchInput.select();
}

function populateNestedEntityDropdown(rowId, currentValue, filterText = '') {
    const dropdown = document.getElementById('nested_entity_dropdown_' + rowId);
    const searchInput = document.getElementById('nested_entity_search_' + rowId);

    if (!dropdown) return;

    dropdown.innerHTML = '';

    // Get all entity IDs and filter them
    const entities = Object.keys(allStates);
    const filteredEntities = entities.filter(entityId =>
        entityId.toLowerCase().includes(filterText.toLowerCase())
    ).sort();

    // Limit results to prevent performance issues
    const maxResults = 100;
    const limitedEntities = filteredEntities.slice(0, maxResults);

    if (limitedEntities.length === 0) {
        dropdown.innerHTML = '<div class="entity-option">No entities found</div>';
        return;
    }

    limitedEntities.forEach(entityId => {
        const entityState = allStates[entityId];
        const entityValue = entityState && entityState.state ? entityState.state : 'unknown';
        const unit_of_measurement = entityState && entityState.unit_of_measurement ? entityState.unit_of_measurement : '';

        const option = document.createElement('div');
        option.className = 'entity-option';
        option.dataset.entityId = entityId;

        // Highlight current selection (only if search input is empty or matches exactly)
        if (entityId === currentValue && (!filterText || filterText === entityId)) {
            option.classList.add('selected');
        }

        option.innerHTML = `
            <div class="entity-name">${entityId}</div>
            <div class="entity-value">${entityValue}${unit_of_measurement}</div>
        `;

        option.addEventListener('click', () => {
            // Clear previous selections
            dropdown.querySelectorAll('.entity-option').forEach(opt => opt.classList.remove('selected'));
            // Select this option
            option.classList.add('selected');
            searchInput.value = entityId;
        });

        dropdown.appendChild(option);
    });

    if (filteredEntities.length > maxResults) {
        const moreOption = document.createElement('div');
        moreOption.className = 'entity-option';
        moreOption.style.fontStyle = 'italic';
        moreOption.innerHTML = `<div class="entity-name">... and ${filteredEntities.length - maxResults} more (refine search)</div>`;
        dropdown.appendChild(moreOption);
    }
}

function setupNestedEntitySearch(rowId, currentValue) {
    const searchInput = document.getElementById('nested_entity_search_' + rowId);

    if (!searchInput) return;

    // Set initial value if it's an entity
    if (currentValue && currentValue.includes('.')) {
        searchInput.value = currentValue;
    }

    // Handle search input
    searchInput.addEventListener('input', (e) => {
        const filterText = e.target.value;
        populateNestedEntityDropdown(rowId, currentValue, filterText);
    });

    // Handle keyboard navigation
    searchInput.addEventListener('keydown', (e) => {
        const dropdown = document.getElementById('nested_entity_dropdown_' + rowId);
        const options = dropdown.querySelectorAll('.entity-option[data-entity-id]');
        const selected = dropdown.querySelector('.entity-option.selected');

        let newSelection = null;

        if (e.key === 'ArrowDown') {
            e.preventDefault();
            if (selected) {
                newSelection = selected.nextElementSibling;
                if (!newSelection || !newSelection.dataset.entityId) {
                    newSelection = options[0];
                }
            } else {
                newSelection = options[0];
            }
        } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            if (selected) {
                newSelection = selected.previousElementSibling;
                if (!newSelection || !newSelection.dataset.entityId) {
                    newSelection = options[options.length - 1];
                }
            } else {
                newSelection = options[options.length - 1];
            }
        } else if (e.key === 'Enter') {
            e.preventDefault();
            if (selected && selected.dataset.entityId) {
                searchInput.value = selected.dataset.entityId;
            }
            saveNestedEntityValue(rowId);
            return;
        } else if (e.key === 'Escape') {
            e.preventDefault();
            cancelNestedEdit(rowId);
            return;
        }

        if (newSelection) {
            // Clear previous selections
            options.forEach(opt => opt.classList.remove('selected'));
            // Select new option
            newSelection.classList.add('selected');
            // Scroll into view if needed
            newSelection.scrollIntoView({ block: 'nearest' });
        }
    });
}

function saveNestedEntityValue(rowId) {
    const row = document.getElementById('nested_row_' + rowId);
    const searchInput = document.getElementById('nested_entity_search_' + rowId);
    const nestedPath = row.dataset.nestedPath;
    const newValue = searchInput.value.trim();
    const originalValue = row.dataset.nestedOriginal;

    // Validate the input
    if (newValue === '') {
        showMessage('Entity value cannot be empty', 'error');
        return;
    }

    // Validate that it's a valid entity ID (contains at least one dot)
    if (!newValue.includes('.')) {
        showMessage('Please select a valid entity ID', 'error');
        return;
    }

    // New value without text after dollar (if existing)
    const newValueBase = newValue.split('$')[0].trim();

    // Check if entity exists in allStates
    if (!allStates[newValueBase]) {
        if (!confirm(`Entity "${newValueBase}" was not found in Home Assistant. Do you want to use it anyway?`)) {
            return;
        }
    }

    // Track the change locally
    if (newValue !== originalValue) {
        pendingChanges[nestedPath] = {
            rowId: rowId,
            originalValue: originalValue,
            newValue: newValue,
            type: 'entity',
            isNested: true,
            path: nestedPath
        };
        markNestedRowAsChanged(rowId);
    } else {
        // If value is same as original, remove from pending changes
        if (pendingChanges[nestedPath]) {
            delete pendingChanges[nestedPath];
            unmarkNestedRowAsChanged(rowId);
        }
    }

    // Update the display value - show entity's current state value, not just the entity ID
    const valueCell = document.getElementById('nested_value_' + rowId);

    // Get the entity's current state value for display
    valueCell.innerHTML = getDisplayValueEntity(newValue);

    updateChangeCounter();
}

function showConfirmationDialog() {
    const changeCount = Object.keys(pendingChanges).length;

    // Create the overlay
    const overlay = document.createElement('div');
    overlay.className = 'confirmation-overlay';
    overlay.innerHTML = `
        <div class="confirmation-dialog">
            <h3>⚠️ Confirm Save Changes</h3>
            <p>You are about to save <strong>${changeCount}</strong> change${changeCount > 1 ? 's' : ''} to apps.yaml.</p>
            <p><strong>Warning:</strong> This will restart Predbat to apply the changes.</p>
            <div class="confirmation-buttons">
                <button class="cancel-button-dialog" onclick="hideConfirmationDialog()">Cancel</button>
                <button class="confirm-button" onclick="confirmSaveChanges()">Save & Restart</button>
            </div>
        </div>
    `;

    document.body.appendChild(overlay);
}

function hideConfirmationDialog() {
    const overlay = document.querySelector('.confirmation-overlay');
    if (overlay) {
        overlay.remove();
    }
}

async function confirmSaveChanges() {
    hideConfirmationDialog();

    // Disable save buttons
    document.getElementById('saveAllButton').disabled = true;
    document.getElementById('discardAllButton').disabled = true;

    showMessage('Saving changes...', 'success');

    // Send all changes to the server
    try {
        const formData = new FormData();
        formData.append('changes', JSON.stringify(pendingChanges));

        const response = await fetch('./apps', {
            method: 'POST',
            body: formData
        });

        const result = await response.json();

        if (result.success) {
            showMessage(result.message + ' - Page will reload...', 'success');

            // Clear pending changes
            pendingChanges = {};
            updateChangeCounter();

            // Reload the page after a short delay
            setTimeout(() => {
                window.location.reload();
            }, 2000);
        } else {
            showMessage(result.message, 'error');
            // Re-enable buttons
            document.getElementById('saveAllButton').disabled = false;
            document.getElementById('discardAllButton').disabled = false;
        }
    } catch (error) {
        showMessage('Error saving changes: ' + error.message, 'error');
        // Re-enable buttons
        document.getElementById('saveAllButton').disabled = false;
        document.getElementById('discardAllButton').disabled = false;
    }
}

function saveAllChanges() {
    if (Object.keys(pendingChanges).length === 0) {
        showMessage('No changes to save', 'error');
        return;
    }

    showConfirmationDialog();
}

// Functions for handling nested dictionary values
function toggleNestedValue(rowId) {
    const row = document.getElementById('nested_row_' + rowId);
    const nestedPath = row.dataset.nestedPath;
    const toggleButton = row.querySelector('.toggle-button');
    const currentValue = toggleButton.dataset.value === 'true';
    const newValue = !currentValue;

    // Track the change locally
    pendingChanges[nestedPath] = {
        rowId: rowId,
        originalValue: row.dataset.nestedOriginal,
        newValue: newValue.toString(),
        type: 'boolean',
        isNested: true,
        path: nestedPath
    };

    // Update the toggle button state visually
    toggleButton.dataset.value = newValue.toString();
    if (newValue) {
        toggleButton.classList.add('active');
    } else {
        toggleButton.classList.remove('active');
    }

    // Update the value display
    const valueCell = document.getElementById('nested_value_' + rowId);
    valueCell.innerHTML = newValue.toString();

    // Mark row as changed and update counter
    markNestedRowAsChanged(rowId);
    updateChangeCounter();
}

function editNestedValue(rowId) {
    const row = document.getElementById('nested_row_' + rowId);
    const valueCell = document.getElementById('nested_value_' + rowId);
    const nestedPath = row.dataset.nestedPath;
    const originalValue = row.dataset.nestedOriginal;

    // Check if there's a pending change, use that value instead of original
    const currentValue = pendingChanges[nestedPath] ? pendingChanges[nestedPath].newValue : originalValue;

    // Check if this is an entity string (contains dots)
    if (currentValue && currentValue.match(/^[a-zA-Z]+\\.\\S+/)) {
        // Show entity dropdown for nested values
        showNestedEntityDropdown(rowId, currentValue);
    } else {
        // Replace the value cell content with an input field for non-entity values
        valueCell.innerHTML = `
            : <input type="text" class="edit-input" id="nested_input_${rowId}" value="${currentValue}">
            <button class="save-button" onclick="saveNestedValue(${rowId})">Apply</button>
            <button class="cancel-button" onclick="cancelNestedEdit(${rowId})">Cancel</button>
        `;

        // Focus the input field
        document.getElementById('nested_input_' + rowId).focus();
    }
}

function cancelNestedEdit(rowId) {
    const row = document.getElementById('nested_row_' + rowId);
    const valueCell = document.getElementById('nested_value_' + rowId);
    const nestedPath = row.dataset.nestedPath;

    // Check if there's a pending change for this nested value
    if (pendingChanges[nestedPath]) {
        // Show the pending value
        valueCell.innerHTML = getDisplayValueEntity(pendingChanges[nestedPath].newValue);
    } else {
        // Show the original value
        const originalValue = row.dataset.nestedOriginal;
        valueCell.innerHTML = getDisplayValueEntity(originalValue);
    }
}

function saveNestedValue(rowId) {
    const row = document.getElementById('nested_row_' + rowId);
    const input = document.getElementById('nested_input_' + rowId);
    const nestedPath = row.dataset.nestedPath;
    const newValue = input.value.trim();
    const originalValue = row.dataset.nestedOriginal;

    // Validate the input
    if (newValue === '') {
        showMessage('Value cannot be empty', 'error');
        return;
    }

    // Determine the value type and validate accordingly
    let valueType = determineValueType(originalValue);
    if (valueType === 'numerical' && newValue !== originalValue) {
        if (!typeIsNumerical(newValue)) {
            showMessage('Invalid number format', 'error');
            return;
        }
    }

    // Track the change locally
    if (newValue !== originalValue) {
        pendingChanges[nestedPath] = {
            rowId: rowId,
            originalValue: originalValue,
            newValue: newValue,
            type: valueType,
            isNested: true,
            path: nestedPath
        };
        markNestedRowAsChanged(rowId);
    } else {
        // If value is same as original, remove from pending changes
        if (pendingChanges[nestedPath]) {
            delete pendingChanges[nestedPath];
            unmarkNestedRowAsChanged(rowId);
        }
    }

    // Update the display value
    const valueCell = document.getElementById('nested_value_' + rowId);
    valueCell.innerHTML = getDisplayValueEntity(newValue);

    updateChangeCounter();
}

function markNestedRowAsChanged(rowId) {
    const row = document.getElementById('nested_row_' + rowId);
    row.classList.add('row-changed');
}

function unmarkNestedRowAsChanged(rowId) {
    const row = document.getElementById('nested_row_' + rowId);
    row.classList.remove('row-changed');
}

// Update the discardAllChanges function to handle nested values
function discardAllChanges() {
    // Reset all changed rows to their original values
    for (const pathOrArgName in pendingChanges) {
        const change = pendingChanges[pathOrArgName];

        if (change.isNested) {
            // Handle nested values
            const row = document.getElementById('nested_row_' + change.rowId);
            const valueCell = document.getElementById('nested_value_' + change.rowId);

            if (change.type === 'boolean') {
                // Reset toggle button
                const toggleButton = row.querySelector('.toggle-button');
                const originalValue = change.originalValue === 'True';
                toggleButton.dataset.value = change.originalValue.toLowerCase();
                if (originalValue) {
                    toggleButton.classList.add('active');
                } else {
                    toggleButton.classList.remove('active');
                }
                valueCell.innerHTML = getDisplayValueEntity(change.originalValue);
            } else {
                // Reset numerical value
                valueCell.innerHTML = getDisplayValueEntity(change.originalValue);
            }

            unmarkNestedRowAsChanged(change.rowId);
        } else {
            // Handle top-level values (existing logic)
            const row = document.getElementById('row_' + change.rowId);
            const valueCell = document.getElementById('value_' + change.rowId);

            if (change.type === 'boolean') {
                // Reset toggle button
                const toggleButton = row.querySelector('.toggle-button');
                const originalValue = change.originalValue === 'True';
                toggleButton.dataset.value = change.originalValue.toLowerCase();
                if (originalValue) {
                    toggleButton.classList.add('active');
                } else {
                    toggleButton.classList.remove('active');
                }
                valueCell.innerHTML = getDisplayValueEntity(change.originalValue);
            } else {
                // Reset numerical or entity value
                valueCell.innerHTML = getDisplayValueEntity(change.originalValue);
            }

            unmarkRowAsChanged(change.rowId);
        }
    }

    // Clear all pending changes
    pendingChanges = {};
    updateChangeCounter();
    showMessage('All changes discarded', 'success');
}
</script>
"""
    )
    return text


def get_html_config_css():
    text = """
        <style>
        .filter-container {
            margin: 20px 0;
            display: flex;
            align-items: center;
        }
        .filter-input {
            padding: 8px 12px;
            border: 1px solid #ccc;
            border-radius: 4px;
            font-size: 16px;
            width: 300px;
            margin-left: 10px;
        }
        body.dark-mode .filter-input {
            background-color: #333;
            color: #e0e0e0;
            border-color: #555;
        }
        </style>
        <script>
        // Save and restore filter value between page loads
        function saveFilterValue() {
            localStorage.setItem('configFilterValue', document.getElementById('configFilter').value);
        }

        function restoreFilterValue() {
            const savedFilter = localStorage.getItem('configFilterValue');
            if (savedFilter) {
                document.getElementById('configFilter').value = savedFilter;
                filterConfig();
            }
        }

        function filterConfig() {
            const filterValue = document.getElementById('configFilter').value.toLowerCase();
            const rows = document.querySelectorAll('#configTable tr');

            // Save filter value for persistence
            saveFilterValue();

            // Skip header row
            for(let i = 1; i < rows.length; i++) {
                const row = rows[i];
                const nameCell = row.querySelector('td:nth-child(2)');
                const entityCell = row.querySelector('td:nth-child(3)');

                if (!nameCell || !entityCell) continue;

                const nameText = nameCell.textContent.toLowerCase();
                const entityText = entityCell.textContent.toLowerCase();

                if (nameText.includes(filterValue) || entityText.includes(filterValue)) {
                    row.style.display = '';
                } else {
                    row.style.display = 'none';
                }
            }
        }

        // Register event to restore filter value after page load
        document.addEventListener('DOMContentLoaded', restoreFilterValue);
        </script>
    """
    return text


def get_apps_css():
    text = """
<style>
.edit-button {
    background-color: #4CAF50;
    color: white;
    border: none;
    padding: 4px 8px;
    text-align: center;
    text-decoration: none;
    display: inline-block;
    font-size: 12px;
    margin: 2px 2px;
    cursor: pointer;
    border-radius: 3px;
}

.edit-button:hover {
    background-color: #45a049;
}

.edit-input {
    width: 300px;
    padding: 4px;
    border: 1px solid #ddd;
    border-radius: 3px;
    font-size: 12px;
}

.save-button, .cancel-button {
    background-color: #2196F3;
    color: white;
    border: none;
    padding: 4px 8px;
    text-align: center;
    text-decoration: none;
    display: inline-block;
    font-size: 12px;
    margin: 2px 2px;
    cursor: pointer;
    border-radius: 3px;
}

.cancel-button {
    background-color: #f44336;
}

.save-button:hover {
    background-color: #0b7dda;
}

.cancel-button:hover {
    background-color: #da190b;
}

.message-container {
    padding: 10px;
    margin: 10px 0;
    border-radius: 4px;
    display: none;
}

.message-success {
    background-color: #d4edda;
    color: #155724;
    border: 1px solid #c3e6cb;
}

.message-error {
    background-color: #f8d7da;
    color: #721c24;
    border: 1px solid #f5c6cb;
}

/* Dark mode styles */
body.dark-mode .edit-button {
    background-color: #4CAF50;
    color: white;
}

body.dark-mode .edit-button:hover {
    background-color: #45a049;
}

body.dark-mode .edit-input {
    background-color: #2d2d2d;
    color: #e0e0e0;
    border: 1px solid #555;
}

body.dark-mode .save-button {
    background-color: #2196F3;
    color: white;
}

body.dark-mode .cancel-button {
    background-color: #f44336;
    color: white;
}

body.dark-mode .message-success {
    background-color: #1e3f20;
    color: #7bc97d;
    border: 1px solid #2d5a2f;
}

body.dark-mode .message-error {
    background-color: #3f1e1e;
    color: #f5c6cb;
    border: 1px solid #5a2d2d;
}

/* Toggle button styles */
.toggle-button {
    position: relative;
    display: inline-block;
    width: 60px;
    height: 24px;
    background-color: #ccc;
    border-radius: 12px;
    cursor: pointer;
    transition: background-color 0.3s;
    border: none;
    outline: none;
}

.toggle-button.active {
    background-color: #f44336;
}

.toggle-button::before {
    content: '';
    position: absolute;
    top: 2px;
    left: 2px;
    width: 20px;
    height: 20px;
    background-color: white;
    border-radius: 50%;
    transition: transform 0.3s;
    box-shadow: 0 2px 4px rgba(0,0,0,0.2);
}

.toggle-button.active::before {
    transform: translateX(36px);
}

.toggle-button:hover {
    opacity: 0.8;
}

/* Dark mode toggle styles */
body.dark-mode .toggle-button {
    background-color: #555 !important;
}

body.dark-mode .toggle-button.active {
    background-color: #f44336 !important;
}

body.dark-mode .toggle-button::before {
    background-color: #e0e0e0 !important;
}

/* Save controls styles */
.save-controls {
    background-color: #f8f9fa;
    border: 1px solid #dee2e6;
    border-radius: 8px;
    padding: 15px;
    margin: 10px 0;
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-wrap: wrap;
    gap: 10px;
}

.save-status {
    display: flex;
    align-items: center;
    font-weight: bold;
    color: #495057;
}

.save-all-button {
    background-color: #28a745;
    color: white;
    border: none;
    padding: 10px 20px;
    border-radius: 5px;
    cursor: pointer;
    font-size: 14px;
    font-weight: bold;
    transition: background-color 0.3s;
}

.save-all-button:hover:not(:disabled) {
    background-color: #218838;
}

.save-all-button:disabled {
    background-color: #6c757d;
    cursor: not-allowed;
}

.discard-all-button {
    background-color: #dc3545;
    color: white;
    border: none;
    padding: 10px 20px;
    border-radius: 5px;
    cursor: pointer;
    font-size: 14px;
    font-weight: bold;
    transition: background-color 0.3s;
}

.discard-all-button:hover:not(:disabled) {
    background-color: #c82333;
}

.discard-all-button:disabled {
    background-color: #6c757d;
    cursor: not-allowed;
}

/* Highlight changed rows */
.row-changed {
    background-color: #fff3cd !important;
    border-left: 4px solid #ffc107 !important;
}

/* Dark mode save controls styles */
body.dark-mode .save-controls {
    background-color: #2d2d2d;
    border: 1px solid #404040;
}

body.dark-mode .save-status {
    color: #e0e0e0;
}

body.dark-mode .row-changed {
    background-color: #3d3d1d !important;
    border-left: 4px solid #ffc107 !important;
}

/* Confirmation dialog styles */
.confirmation-overlay {
    position: fixed;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    background-color: rgba(0, 0, 0, 0.5);
    display: flex;
    justify-content: center;
    align-items: center;
    z-index: 9999;
}

.confirmation-dialog {
    background-color: white;
    border-radius: 8px;
    padding: 25px;
    max-width: 600px;
    width: 95%;
    max-height: 90vh;
    overflow-y: auto;
    box-shadow: 0 4px 20px rgba(0, 0, 0, 0.3);
    word-wrap: break-word;
}

.confirmation-dialog h3 {
    margin-top: 0;
    color: #dc3545;
    font-size: 18px;
    word-wrap: break-word;
}

.confirmation-dialog p {
    margin: 15px 0;
    line-height: 1.6;
    word-wrap: break-word;
    overflow-wrap: break-word;
}

.confirmation-buttons {
    display: flex;
    gap: 10px;
    justify-content: flex-end;
    margin-top: 20px;
}

.confirm-button, .cancel-button-dialog {
    padding: 10px 20px;
    border: none;
    border-radius: 5px;
    cursor: pointer;
    font-size: 14px;
    font-weight: bold;
}

.confirm-button {
    background-color: #dc3545;
    color: white;
}

.confirm-button:hover {
    background-color: #c82333;
}

.cancel-button-dialog {
    background-color: #6c757d;
    color: white;
}

.cancel-button-dialog:hover {
    background-color: #545b62;
}

/* Dark mode confirmation dialog */
body.dark-mode .confirmation-dialog {
    background-color: #2d2d2d;
    color: #e0e0e0;
}

body.dark-mode .confirmation-dialog h3 {
    color: #ff6b6b;
}

/* Entity dropdown styles */
.entity-dropdown-container {
    position: relative;
    width: 100%;
    min-width: 400px;
}

.entity-search-input {
    width: 100%;
    min-width: 400px;
    padding: 8px;
    border: 1px solid #ddd;
    border-radius: 4px;
    font-size: 14px;
    box-sizing: border-box;
}

.entity-dropdown {
    position: absolute;
    top: 100%;
    left: 0;
    right: 0;
    min-width: 400px;
    background: white;
    border: 1px solid #ddd;
    border-top: none;
    max-height: 200px;
    overflow-y: auto;
    z-index: 1000;
    box-shadow: 0 2px 4px rgba(0,0,0,0.1);
}

.entity-option {
    padding: 8px;
    cursor: pointer;
    border-bottom: 1px solid #eee;
    display: flex;
    justify-content: space-between;
    align-items: center;
}

.entity-option:hover,
.entity-option.selected {
    background-color: #f0f0f0;
}

.entity-name {
    font-weight: bold;
    flex: 1;
    margin-right: 10px;
    word-break: break-all;
}

.entity-value {
    color: #666;
    font-size: 12px;
    flex-shrink: 0;
    max-width: 150px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}

/* Dark mode entity dropdown styles */
body.dark-mode .entity-search-input {
    background-color: #444;
    color: #fff;
    border: 1px solid #666;
}

body.dark-mode .entity-dropdown {
    background: #333;
    border: 1px solid #666;
    color: #fff;
}

body.dark-mode .entity-option {
    border-bottom: 1px solid #555;
}

body.dark-mode .entity-option:hover,
body.dark-mode .entity-option.selected {
    background-color: #555;
}

body.dark-mode .entity-value {
    color: #ccc;
}
</style>
"""
    return text


def get_components_css():
    """
    Return CSS for components page
    """
    text = """
<style>
.components-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(400px, 1fr));
    gap: 20px;
    margin: 20px 0;
}

.component-card {
    border: 2px solid #ddd;
    border-radius: 8px;
    padding: 20px;
    background: #fff;
    color: #333;
    box-shadow: 0 2px 4px rgba(0,0,0,0.1);
    transition: border-color 0.3s ease;
    overflow-wrap: break-word;
    word-wrap: break-word;
}

.component-card.active {
    border-color: #4CAF50;
}

.component-card.inactive {
    border-color: #999;
    background: #f9f9f9;
    color: #333;
}

.component-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 15px;
    flex-wrap: wrap;
    gap: 10px;
}

.component-header h3 {
    margin: 0;
    color: #333;
    font-size: 1.2em;
}

.status-indicator {
    font-size: 1.5em;
    margin-right: 8px;
}

.status-healthy {
    color: #4CAF50;
}

.status-error {
    color: #f44336;
}

.status-inactive {
    color: #999;
}

.status-text {
    flex-grow: 1;
    font-weight: bold;
    font-size: 0.9em;
    color: #333;
}

.restart-button {
    background-color: #2196F3;
    color: white;
    border: none;
    padding: 6px 12px;
    border-radius: 4px;
    cursor: pointer;
    font-size: 0.85em;
    font-weight: bold;
    transition: background-color 0.3s ease;
    margin-left: 10px;
}

.restart-button:hover {
    background-color: #1976D2;
}

.restart-button:disabled {
    background-color: #ccc;
    cursor: not-allowed;
}

.component-details {
    border-top: 1px solid #eee;
    padding-top: 15px;
}

.component-details p {
    margin: 8px 0;
    font-size: 0.95em;
    color: #333;
}

.component-args {
    margin-top: 15px;
    overflow: hidden;
}

.component-args h4 {
    margin: 10px 0;
    color: #333;
    font-size: 1em;
}

.args-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.9em;
    margin: 10px 0;
    table-layout: fixed;
}

.args-table th {
    background-color: #f5f5f5;
    padding: 8px;
    text-align: left;
    border: 1px solid #ddd;
    font-weight: bold;
    color: #333;
    word-wrap: break-word;
    overflow-wrap: break-word;
}

.args-table th:nth-child(1) {
    width: 40%;
}

.args-table th:nth-child(2) {
    width: 20%;
}

.args-table th:nth-child(3) {
    width: 40%;
}

.args-table td {
    padding: 8px;
    border: 1px solid #ddd;
    vertical-align: top;
    color: #333;
    word-wrap: break-word;
    overflow-wrap: break-word;
    word-break: break-all;
    max-width: 0;
}

.args-table tr.required-arg {
    background-color: #fff8e1;
}

.args-table tr.optional-arg {
    background-color: #f9f9f9;
}

/* Special handling for long values like URLs */
.args-table td:nth-child(3) {
    word-break: break-all;
    overflow-wrap: anywhere;
    hyphens: auto;
    max-width: 200px;
}

.entity-count-positive {
    color: #4CAF50;
    font-weight: bold;
}

.entity-count-zero {
    color: #999;
    font-weight: bold;
}

.last-updated-time {
    color: #666;
    font-style: italic;
}

/* Dark mode styles */
body.dark-mode .component-card {
    background: #2d2d2d;
    border-color: #555;
    color: #e0e0e0;
}

body.dark-mode .component-card.active {
    border-color: #4CAF50;
}

body.dark-mode .component-card.inactive {
    border-color: #666;
    background: #1e1e1e;
}

body.dark-mode .component-header h3 {
    color: #e0e0e0;
}

body.dark-mode .component-details {
    border-top-color: #555;
}

body.dark-mode .component-details p {
    color: #e0e0e0;
}

body.dark-mode .component-details strong {
    color: #e0e0e0;
}

body.dark-mode .component-args h4 {
    color: #e0e0e0;
}

body.dark-mode .args-table th {
    background-color: #333;
    color: #e0e0e0;
    border-color: #555;
    word-wrap: break-word;
    overflow-wrap: break-word;
}

body.dark-mode .args-table th:nth-child(1) {
    width: 40%;
}

body.dark-mode .args-table th:nth-child(2) {
    width: 20%;
}

body.dark-mode .args-table th:nth-child(3) {
    width: 40%;
}

body.dark-mode .args-table td {
    border-color: #555;
    color: #e0e0e0;
    word-wrap: break-word;
    overflow-wrap: break-word;
    word-break: break-all;
}

body.dark-mode .args-table tr.required-arg {
    background-color: #3a3a1a;
}

body.dark-mode .args-table tr.optional-arg {
    background-color: #2a2a2a;
}

body.dark-mode .entity-count-positive {
    color: #4CAF50;
    font-weight: bold;
}

body.dark-mode .entity-count-zero {
    color: #666;
    font-weight: bold;
}

body.dark-mode .last-updated-time {
    color: #aaa;
    font-style: italic;
}

body.dark-mode .restart-button {
    background-color: #2196F3;
    color: white;
}

body.dark-mode .restart-button:hover {
    background-color: #1976D2;
}

body.dark-mode .restart-button:disabled {
    background-color: #555;
    color: #999;
}

/* Responsive design */
@media (max-width: 768px) {
    .components-grid {
        grid-template-columns: 1fr;
    }

    .component-header {
        flex-direction: column;
        align-items: flex-start;
        gap: 5px;
    }

    .args-table {
        font-size: 0.8em;
    }

    .args-table th,
    .args-table td {
        padding: 6px;
    }
}
</style>
"""
    return text


def get_charts_css():
    text = """
<style>
.charts-menu {
tabindex="0"  <!-- Make the menu focusable -->
    background-color: #ffffff;
    overflow-x: auto; /* Enable horizontal scrolling */
    white-space: nowrap; /* Prevent menu items from wrapping */
    display: flex;
    align-items: center;
    margin-bottom: 6px;
    border-bottom: 1px solid #ddd;
    padding: 4px 0;
    -webkit-overflow-scrolling: touch; /* Smooth scrolling on iOS */
    scrollbar-width: thin; /* Firefox */
    scrollbar-color: #4CAF50 #f0f0f0; /* Firefox */
}

.charts-menu h3 {
    margin: 0 10px;
    flex-shrink: 0; /* Prevent shrinking */
    white-space: nowrap; /* Prevent text wrapping */
}

.charts-menu a {
    color: #333;
    text-align: center;
    padding: 4px 12px;
    text-decoration: none;
    font-size: 14px;
    border-radius: 4px;
    margin: 0 2px;
    flex-shrink: 0; /* Prevent items from shrinking */
    white-space: nowrap;
    display: inline-block;
}

.charts-menu a:hover {
    background-color: #f0f0f0;
    color: #4CAF50;
}

.charts-menu a.active {
    background-color: #4CAF50;
    color: white;
}

/* Dark mode charts menu styles */
body.dark-mode .charts-menu {
    background-color: #1e1e1e;
    border-bottom: 1px solid #333;
    scrollbar-color: #4CAF50 #333; /* Firefox */
}

body.dark-mode .charts-menu h3 {
    color: #e0e0e0;
}

body.dark-mode .charts-menu a {
    color: white;
}

body.dark-mode .charts-menu a:hover {
    background-color: #2c652f;
    color: white;
}

body.dark-mode .charts-menu a.active {
    background-color: #4CAF50;
    color: white;
}
</style>
<script>
// Initialize the charts menu scrolling functionality
document.addEventListener("DOMContentLoaded", function() {
    // Scroll active item into view
    setTimeout(function() {
        const activeItem = document.querySelector('.charts-menu a.active');
        if (activeItem) {
            const menuBar = document.querySelector('.charts-menu');
            const activeItemLeft = activeItem.offsetLeft;
            const menuBarWidth = menuBar.clientWidth;
            menuBar.scrollLeft = activeItemLeft - menuBarWidth / 2 + activeItem.clientWidth / 2;
        }
    }, 100);
});
</script>
"""
    return text


def get_log_css():
    text = """
<style>
.log-menu {
    background-color: #ffffff;
    overflow: hidden;
    display: flex;
    align-items: center;
    margin-bottom: 6px;
    border-bottom: 1px solid #ddd;
    padding: 4px 0;
}

.log-menu a {
    color: #333;
    text-align: center;
    padding: 4px 12px;
    text-decoration: none;
    font-size: 14px;
    border-radius: 4px;
    margin: 0 2px;
}

.log-menu a:hover {
    background-color: #f0f0f0;
    color: #4CAF50;
}

.log-menu a.active {
    background-color: #4CAF50;
    color: white;
}

/* Dark mode log menu styles */
body.dark-mode .log-menu {
    background-color: #1e1e1e;
    border-bottom: 1px solid #333;
}

body.dark-mode .log-menu a {
    color: white;
}

body.dark-mode .log-menu a:hover {
    background-color: #2c652f;
    color: white;
}

body.dark-mode .log-menu a.active {
    background-color: #4CAF50;
    color: white;
}

/* Log search container styles */
.log-search-container {
    display: flex;
    align-items: center;
    margin-bottom: 10px;
    padding: 8px;
    background-color: #f8f9fa;
    border: 1px solid #ddd;
    border-radius: 4px;
    gap: 8px;
}

.log-search-input {
    flex-grow: 1;
    padding: 6px 10px;
    border: 1px solid #ccc;
    border-radius: 4px;
    font-size: 14px;
    min-width: 200px;
}

.log-search-input:focus {
    outline: none;
    border-color: #4CAF50;
    box-shadow: 0 0 0 2px rgba(76, 175, 80, 0.2);
}

.clear-search-button {
    padding: 6px 12px;
    background-color: #6c757d;
    color: white;
    border: none;
    border-radius: 4px;
    cursor: pointer;
    font-size: 14px;
    white-space: nowrap;
}

.clear-search-button:hover {
    background-color: #5a6268;
}

.search-status {
    font-size: 12px;
    color: #6c757d;
    white-space: nowrap;
}

/* Dark mode search styles */
body.dark-mode .log-search-container {
    background-color: #2d3748;
    border-color: #4a5568;
}

body.dark-mode .log-search-input {
    background-color: #4a5568;
    border-color: #718096;
    color: #fff;
}

body.dark-mode .log-search-input:focus {
    border-color: #4CAF50;
}

body.dark-mode .log-search-input::placeholder {
    color: #a0aec0;
}

body.dark-mode .clear-search-button {
    background-color: #4a5568;
}

body.dark-mode .clear-search-button:hover {
    background-color: #2d3748;
}

body.dark-mode .search-status {
    color: #a0aec0;
}

/* Hide filtered log entries */
.log-entry-hidden {
    display: none !important;
}

/* Highlight matching text */
mark.search-highlight {
    background-color: #ffeb3b;
    color: #000;
    font-weight: bold;
    padding: 1px 2px;
    border-radius: 2px;
    box-shadow: 0 1px 2px rgba(0,0,0,0.1);
}

body.dark-mode mark.search-highlight {
    background-color: #ffa000;
    color: #000;
    font-weight: bold;
    padding: 1px 2px;
    border-radius: 2px;
    box-shadow: 0 1px 2px rgba(255,255,255,0.1);
}
</style>
"""

    # Add custom CSS for live updates
    text += """
        <style>
        .log-status {
            margin: 10px 0;
            padding: 8px;
            background-color: #f0f0f0;
            border-radius: 4px;
            font-size: 12px;
        }
        body.dark-mode .log-status {
            background-color: #333;
            color: #fff;
        }
        .new-log-entry {
            animation: highlight 2s ease-out;
        }
        @keyframes highlight {
            0% { background-color: #ffff99; }
            100% { background-color: transparent; }
        }
        body.dark-mode .new-log-entry {
            animation: highlight-dark 2s ease-out;
        }
        @keyframes highlight-dark {
            0% { background-color: #555500; }
            100% { background-color: transparent; }
        }
        .auto-scroll-toggle {
            margin-left: 10px;
            cursor: pointer;
        }
        .scroll-to-bottom {
            margin-left: 10px;
            padding: 4px 8px;
            background: #007cba;
            color: white;
            border: none;
            border-radius: 4px;
            cursor: pointer;
            font-size: 12px;
        }
        .scroll-to-bottom:hover {
            background: #005a87;
        }
        </style>
    """

    return text


def get_editor_css():
    text = """
<style>
.editor-container {
    position: fixed;
    top: 70px; /* Account for fixed header */
    left: 0;
    right: 0;
    bottom: 0;
    display: flex;
    flex-direction: column;
    background-color: inherit;
    z-index: 10;
}

.editor-header {
    flex-shrink: 0;
    padding: 10px 15px;
    margin: 0;
    background-color: inherit;
}

.editor-form {
    flex: 1;
    display: flex;
    flex-direction: column;
    padding: 0 15px 15px 15px;
    min-height: 0; /* Important for flex children */
    overflow: hidden; /* Prevent form from overflowing */
}

.editor-textarea {
    flex: 1;
    font-family: 'Courier New', monospace;
    font-size: 14px;
    line-height: 1.4;
    padding: 10px;
    border: 2px solid #4CAF50;
    border-radius: 4px;
    resize: none;
    background-color: #ffffff;
    color: #333;
    white-space: pre;
    overflow-wrap: normal;
    overflow: auto;
    min-height: 0; /* Important for flex children */
    width: 100%;
    box-sizing: border-box;
}

.editor-controls {
    flex-shrink: 0;
    margin-top: 10px;
    display: flex;
    gap: 10px;
    align-items: center;
    flex-wrap: wrap;
    padding: 10px 0;
}

.save-button {
    background-color: #4CAF50;
    color: white;
    border: none;
    padding: 12px 24px;
    font-size: 16px;
    border-radius: 4px;
    cursor: pointer;
    font-weight: bold;
}

.save-button:hover {
    background-color: #45a049;
}

.save-button:disabled {
    background-color: #cccccc !important;
    color: #888888 !important;
    cursor: not-allowed !important;
    position: relative !important;
    border: 2px solid #bbbbbb !important;
}

.revert-button {
    background-color: #f44336;
    color: white;
    border: none;
    padding: 12px 24px;
    font-size: 16px;
    border-radius: 4px;
    cursor: pointer;
    font-weight: bold;
}

.revert-button:hover {
    background-color: #d32f2f;
}

.revert-button:disabled {
    background-color: #cccccc !important;
    color: #888888 !important;
    cursor: not-allowed !important;
    position: relative !important;
    border: 2px solid #bbbbbb !important;
}

.message {
    padding: 10px;
    border-radius: 4px;
    margin: 10px 0;
    display: none;
}

.success {
    background-color: #d4edda;
    color: #155724;
    border: 1px solid #c3e6cb;
}

.error {
    background-color: #f8d7da;
    color: #721c24;
    border: 1px solid #f5c6cb;
}

/* CodeMirror specific styles */
.CodeMirror {
    height: auto;
    flex: 1;
    font-family: 'Courier New', monospace;
    font-size: 14px;
    border: 2px solid #4CAF50;
    border-radius: 4px;
    background-color: #ffffff !important; /* Force white background */
}

/* Selection style */
.CodeMirror-selected {
    background-color: #b5d5ff !important;
}

/* Dark mode selection style */
body.dark-mode .CodeMirror-selected {
    background-color: #3a3d41 !important;
}

.CodeMirror-gutters {
    background-color: #f8f8f8;
    border-right: 1px solid #ddd;
}

.CodeMirror-linenumber {
    color: #999;
}

/* Dark mode styles */
body.dark-mode .editor-textarea {
    background-color: #2d2d2d;
    color: #f0f0f0;
    border-color: #4CAF50;
}

body.dark-mode .CodeMirror {
    border-color: #4CAF50;
    background-color: #2d2d2d !important; /* Force dark background */
    color: #f0f0f0 !important; /* Force light text */
}

body.dark-mode .CodeMirror-gutters {
    background-color: #2d2d2d;
    border-right: 1px solid #444;
}

body.dark-mode .CodeMirror-linenumber {
    color: #777;
}

/* Dark mode syntax highlighting adjustments */
body.dark-mode .cm-s-default .cm-string {
    color: #ce9178 !important;
}

body.dark-mode .cm-s-default .cm-number {
    color: #b5cea8 !important;
}

body.dark-mode .cm-s-default .cm-keyword {
    color: #569cd6 !important;
}

body.dark-mode .cm-s-default .cm-property {
    color: #9cdcfe !important;
}

body.dark-mode .cm-s-default .cm-atom {
    color: #d19a66 !important;
}

body.dark-mode .cm-s-default .cm-comment {
    color: #6a9955 !important;
}

body.dark-mode .cm-s-default .cm-meta {
    color: #dcdcaa !important;
}

body.dark-mode .cm-s-default .cm-tag {
    color: #569cd6 !important;
}

body.dark-mode .cm-s-default .cm-attribute {
    color: #9cdcfe !important;
}

body.dark-mode .cm-s-default .cm-variable {
    color: #9cdcfe !important;
}

body.dark-mode .cm-s-default .cm-variable-2 {
    color: #4ec9b0 !important;
}

body.dark-mode .cm-s-default .cm-def {
    color: #dcdcaa !important;
}

body.dark-mode .CodeMirror-cursor {
    border-left: 1px solid #f0f0f0 !important;
}

/* Lint markers for both light and dark mode */
.CodeMirror-lint-marker-error, .CodeMirror-lint-message-error {
    background-image: url('data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAyNCAyNCIgd2lkdGg9IjE2IiBoZWlnaHQ9IjE2IiBmaWxsPSJyZWQiPjxwYXRoIGQ9Ik0xMS45OTMgMi4wMDFhMTAgMTAgMCAwMC03LjA3MyAyLjkyOEExMCAxMCAwIDAwMS45OTIgMTIuMDAxYTEwIDEwIDAgMDAyLjkyOCA3LjA3MiAxMCAxMCAwIDAwNy4wNzMgMi45MjkgMTAgMTAgMCAwMDcuMDczLTIuOTMgMTAgMTAgMCAwMDIuOTI4LTcuMDcxIDEwIDEwIDAgMDAtMi45MjgtNy4wNzIgMTAgMTAgMCAwMC03LjA3My0yLjkyOHptMCA0bC4yMzIuMDAzYy41MjUuMDEzLjk5NC4zMzQgMS4yLjgyNWwuMDQuMTAzTDE2LjM0NSAxNWExLjUgMS41IDAgMDEtMi44NS45NDVsLS4wMzItLjFMMTIgMTIuNzYzIDguNTM3IDE1LjgybC0uMDk2LjA4YTEuNSAxLjUgMCAwMS0xLjgxLjEwNmwtLjEwMi0uMDgxYTEuNSAxLjUgMCAwMS0uMTA4LTEuODA2bC4wOC0uMTA0TDkuNCA3Ljk0NWwuMDgxLS4xMjVjLjIzMS0uMzE2LjYxNi0uNTE2IDEuMDM3LS4wMTZsLjA5Mi4wODMuMDc4LjA5My4wOTcuMTM2LjA0OC4wODUuMTYuMDMyeloiLz48L3N2Zz4=');
    background-position: center;
    background-repeat: no-repeat;
}

.CodeMirror-lint-tooltip {
    border: 1px solid #ccc;
    border-radius: 4px;
    background-color: white;
    z-index: 10000;
    max-width: 600px;
    overflow: hidden;
    white-space: pre-wrap;
    padding: 8px;
    box-shadow: 0 2px 10px rgba(0, 0, 0, 0.2);
    color: #333;
}

body.dark-mode .CodeMirror-lint-tooltip {
    background-color: #2d2d2d;
    color: #f0f0f0;
    border-color: #444;
}

.CodeMirror-lint-marker-error:hover {
    cursor: pointer;
}

.CodeMirror-lint-line-error {
    background-color: rgba(255, 0, 0, 0.1);
}

body.dark-mode .CodeMirror-lint-line-error {
    background-color: rgba(255, 0, 0, 0.2);
}

body.dark-mode .message.success {
    background-color: #1e3f20;
    color: #7bc97d;
    border-color: #2d5a2f;
}

body.dark-mode .message.error {
    background-color: #3f1e1e;
    color: #f5c6cb;
    border-color: #5a2d2d;
}

body.dark-mode .save-button:disabled {
    background-color: #444444 !important;
    color: #777777 !important;
    border: 2px solid #555555 !important;
}

body.dark-mode .revert-button {
    background-color: #c62828;
    color: white;
    border: none;
}

body.dark-mode .revert-button:hover {
    background-color: #b71c1c;
}

body.dark-mode .revert-button:disabled {
    background-color: #444444 !important;
    color: #777777 !important;
    border: 2px solid #555555 !important;
}
</style>"""
    return text


def get_logfile_js(filter_type):
    """
    Return JavaScript for log file page
    """
    text = f"""
        <script>
        let currentFilter = '{filter_type}';
        let lastLineNumber = 0;
        let updateInterval;
        let isUpdating = false;
        let isPaused = false;
        let searchTimeout = null; // For debouncing search

        // Toggle pause/resume updates
        function toggleUpdates() {{
            const btn = document.getElementById('pauseResumeBtn');
            if (isPaused) {{
                // Resume
                isPaused = false;
                btn.textContent = 'Pause';
                updateInterval = setInterval(updateLog, 2000);
                updateLog(); // Immediate update
                updateStatus('Updates resumed');
            }} else {{
                // Pause
                isPaused = true;
                btn.textContent = 'Resume';
                if (updateInterval) {{
                    clearInterval(updateInterval);
                    updateInterval = null;
                }}
                updateStatus('Updates paused');
            }}
        }}

        // Get the highest line number currently displayed
        function getLastLineNumber() {{
            const rows = document.querySelectorAll('#logTableBody tr[data-line]');
            let maxLine = 0;
            rows.forEach(row => {{
                const lineNum = parseInt(row.getAttribute('data-line'));
                if (lineNum > maxLine) {{
                    maxLine = lineNum;
                }}
            }});
            return maxLine;
        }}

        // Update log status
        function updateStatus(message) {{
            const statusDiv = document.getElementById('logStatus');
            if (statusDiv) {{
                const now = new Date().toLocaleTimeString();
                statusDiv.textContent = `${{message}} (Last updated: ${{now}})`;
            }}
        }}

        // Escape HTML to prevent XSS
        function escapeHtml(text) {{
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }}

        // Add new log entries
        function addLogEntries(lines) {{
            const tbody = document.getElementById('logTableBody');
            if (!tbody) return 0;

            const autoScroll = document.getElementById('autoScroll');
            const shouldAutoScroll = autoScroll ? autoScroll.checked : false;
            let newEntriesAdded = 0;

            // Insert entries at the top (newest first)
            lines.forEach(logLine => {{
                if (logLine.line_number > lastLineNumber) {{
                    const row = document.createElement('tr');
                    row.setAttribute('data-line', logLine.line_number);
                    row.className = 'new-log-entry';

                    let color = '#33cc33'; // Default green for info
                    if (logLine.type === 'error') {{
                        color = '#ff3333';
                    }} else if (logLine.type === 'warning') {{
                        color = '#ffA500';
                    }}

                    // Use highlighted content from server (already HTML-escaped and highlighted)
                    const timestamp = logLine.timestamp;
                    const message = logLine.message;

                    row.innerHTML = `<td>${{logLine.line_number}}</td><td nowrap><font color="${{color}}">${{timestamp}}</font> ${{message}}</td>`;

                    // Insert at the top of the table body
                    tbody.insertBefore(row, tbody.firstChild);
                    newEntriesAdded++;

                    lastLineNumber = Math.max(lastLineNumber, logLine.line_number);
                }}
            }});

            // Auto-scroll to top for new entries (since newest are at top)
            if (newEntriesAdded > 0 && shouldAutoScroll) {{
                setTimeout(() => {{
                    window.scrollTo({{
                        top: 0,
                        behavior: 'smooth'
                    }});
                }}, 100);
            }}

            return newEntriesAdded;
        }}

        // Scroll to bottom function
        function scrollToBottom() {{
            window.scrollTo({{
                top: document.body.scrollHeight,
                behavior: 'smooth'
            }});
        }}

        // Debounced search function
        function debouncedSearch() {{
            const searchTerm = document.getElementById('logSearchInput').value.toLowerCase().trim();

            // Clear existing timeout
            if (searchTimeout) {{
                clearTimeout(searchTimeout);
            }}

            // Set new timeout for search (500ms delay)
            searchTimeout = setTimeout(() => {{
                performServerSearch(searchTerm);
            }}, 500);
        }}

        // Search functionality - now uses server-side search with debouncing
        function filterLogEntries() {{
            debouncedSearch();
        }}

        // Perform server-side search
        async function performServerSearch(searchTerm) {{
            if (isUpdating) return;
            isUpdating = true;

            const statusDiv = document.getElementById('searchStatus');
            statusDiv.textContent = 'Searching...';

            try {{
                const searchParam = searchTerm ? `&search=${{encodeURIComponent(searchTerm)}}` : '';
                const response = await fetch(`./api/log?filter=${{currentFilter}}&since=0&max_lines=1024${{searchParam}}`);

                if (!response.ok) {{
                    throw new Error(`HTTP ${{response.status}}: ${{response.statusText}}`);
                }}

                const data = await response.json();

                if (data.status === 'success') {{
                    // Clear existing table content
                    const tbody = document.getElementById('logTableBody');
                    tbody.innerHTML = '';

                    // Reset line number tracking
                    lastLineNumber = 0;

                    // Add search results
                    const entriesAdded = addLogEntries(data.lines);

                    // Update status with search results info
                    if (searchTerm) {{
                        const searchMatches = data.search_matches || data.returned_lines;
                        const displayedResults = data.returned_lines;

                        if (searchMatches > displayedResults) {{
                            statusDiv.textContent = `Found ${{searchMatches}} matching entries (showing ${{displayedResults}})`;
                        }} else {{
                            statusDiv.textContent = `Found ${{searchMatches}} matching entries (showing ${{displayedResults}})`;
                        }}

                        updateStatus(`Search completed - ${{searchMatches}} matches found`);
                    }} else {{
                        statusDiv.textContent = '';
                        updateStatus('Search cleared - showing recent entries');
                    }}
                }} else {{
                    statusDiv.textContent = `Search error: ${{data.message || 'Unknown error'}}`;
                    updateStatus(`Search error: ${{data.message || 'Unknown error'}}`);
                }}
            }} catch (error) {{
                console.error('Error performing search:', error);
                statusDiv.textContent = `Search error: ${{error.message}}`;
                updateStatus(`Search error: ${{error.message}}`);
            }} finally {{
                isUpdating = false;
            }}
        }}

        // Clear search function
        function clearLogSearch() {{
            document.getElementById('logSearchInput').value = '';
            filterLogEntries();
        }}

        // Escape special regex characters
        function escapeRegExp(string) {{
            return string.replace(/[.*+?^${{}}()|[\\]\\\\]/g, '\\\\$&');
        }}

        // Fetch new log entries
        async function updateLog() {{
            if (isUpdating || isPaused) return;

            const searchTerm = document.getElementById('logSearchInput').value.toLowerCase().trim();

            isUpdating = true;

            try {{
                let url;
                if (searchTerm) {{
                    // If search is active, fetch new entries that match the search criteria
                    const searchParam = `&search=${{encodeURIComponent(searchTerm)}}`;
                    url = `./api/log?filter=${{currentFilter}}&since=${{lastLineNumber}}&max_lines=100${{searchParam}}`;
                }} else {{
                    // Normal update without search
                    url = `./api/log?filter=${{currentFilter}}&since=${{lastLineNumber}}&max_lines=1024`;
                }}

                const response = await fetch(url);

                if (!response.ok) {{
                    throw new Error(`HTTP ${{response.status}}: ${{response.statusText}}`);
                }}

                const data = await response.json();

                if (data.status === 'success') {{
                    const newEntries = addLogEntries(data.lines);
                    if (newEntries > 0) {{
                        if (searchTerm) {{
                            updateStatus(`${{newEntries}} new matching entries added`);
                            // Update search status to reflect new matches
                            const statusDiv = document.getElementById('searchStatus');
                            const rows = document.querySelectorAll('#logTableBody tr[data-line]');
                            const totalDisplayed = rows.length;
                            const currentText = statusDiv.textContent;

                            // Try to extract existing match count and update it
                            if (currentText.includes('Found')) {{
                                const match = currentText.match(/Found (\\d+)/);
                                if (match) {{
                                    const oldCount = parseInt(match[1]);
                                    const newCount = oldCount + newEntries;
                                    statusDiv.textContent = `Found ${{newCount}} matching entries (showing ${{totalDisplayed}})`;
                                }}
                            }}
                        }} else {{
                            updateStatus(`${{newEntries}} new entries added`);
                        }}
                    }} else {{
                        if (searchTerm) {{
                            updateStatus('No new matching entries');
                        }} else {{
                            updateStatus('No new entries');
                        }}
                    }}
                }} else {{
                    updateStatus(`Error: ${{data.message || 'Unknown error'}}`);
                }}
            }} catch (error) {{
                console.error('Error updating log:', error);
                updateStatus(`Error fetching log data: ${{error.message}}`);
            }} finally {{
                isUpdating = false;
            }}
        }}

        // Initialize
        document.addEventListener('DOMContentLoaded', function() {{
            lastLineNumber = 0; // Start from 0 since we're loading all initial data
            updateStatus('Log viewer loaded - fetching initial data...');

            // Load initial data immediately
            updateLog();

            // Start periodic updates every 2 seconds
            updateInterval = setInterval(updateLog, 2000);

            // Handle page visibility changes to pause/resume updates
            document.addEventListener('visibilitychange', function() {{
                if (isPaused) return; // Don't auto-resume if manually paused

                if (document.hidden) {{
                    if (updateInterval) {{
                        clearInterval(updateInterval);
                        updateInterval = null;
                    }}
                }} else {{
                    if (!updateInterval) {{
                        updateInterval = setInterval(updateLog, 2000);
                    }}
                    updateLog(); // Immediate update when page becomes visible
                }}
            }});
        }});

        // Clean up on page unload
        window.addEventListener('beforeunload', function() {{
            if (updateInterval) {{
                clearInterval(updateInterval);
                updateInterval = null;
            }}
        }});
        </script>
        """
    return text


def get_editor_js():
    text = """
<script>
let isSubmitting = false;
let editor; // CodeMirror instance

document.getElementById('editorForm').addEventListener('submit', function(e) {
    e.preventDefault(); // Always prevent default initially

    if (isSubmitting) {
        return;
    }

    // Get the current content and validate it
    const content = editor ? editor.getValue() : document.getElementById('appsContent').value;
    let hasYamlError = false;

    try {
        // Only validate if content exists and isn't empty
        if (content && content.trim()) {
            jsyaml.load(content);
        }
    } catch (e) {
        console.log('YAML validation error during form submit:', e.message);
        hasYamlError = true;
    }

    // Safety check - don't allow submission if there are YAML errors
    if (hasYamlError) {
        showMessage("Cannot save while there are YAML syntax errors. Please fix the errors first.", "error");
        return;
    }

    // Show confirmation popup
    const confirmed = confirm("Warning: Saving changes will restart Predbat. Are you sure you want to continue?");

    if (!confirmed) {
        return; // User cancelled the save
    }

    // Update the hidden textarea with CodeMirror content before submission
    if (editor) {
        document.getElementById('appsContent').value = editor.getValue();
    }

    isSubmitting = true;
    const saveButton = document.getElementById('saveButton');
    const saveStatus = document.getElementById('saveStatus');

    saveButton.disabled = true;
    saveButton.textContent = 'Saving...';
    saveStatus.textContent = 'Please wait...';

    // Clear stored content as we're saving now
    localStorage.removeItem('appsYamlContent');

    // Submit the form programmatically
    this.submit();
});

// Show messages
function showMessage(message, type = 'success') {
    const messageContainer = document.getElementById('messageContainer');
    const messageDiv = document.createElement('div');
    messageDiv.className = `message ${type}`;
    messageDiv.style.display = 'block';
    messageDiv.textContent = message;

    messageContainer.innerHTML = '';
    messageContainer.appendChild(messageDiv);

    // Auto-hide success messages after 5 seconds
    if (type === 'success') {
        setTimeout(() => {
            messageDiv.style.display = 'none';
        }, 5000);
    }
}

// Function to update button states based on content changes and validation
function updateButtonStates(saveButton, revertButton, content, hasError = false) {
    if (!saveButton && !revertButton) return;

    const isDarkMode = document.body.classList.contains('dark-mode');
    const hasChanged = content !== window.originalContent;

    // Update Save button
    if (saveButton) {
        // First set the disabled property, which is crucial for behavior
        const shouldDisableSave = hasError || !hasChanged;
        saveButton.disabled = shouldDisableSave;

        // Update tooltip
        if (hasError) {
            saveButton.title = 'Fix YAML errors before saving';
        } else {
            saveButton.title = hasChanged ? 'Save changes' : 'No changes to save';
        }

        // Apply styling - ensure disabled style gets applied correctly
        if (shouldDisableSave) {
            // Disabled styling
            saveButton.style.backgroundColor = isDarkMode ? '#444444' : '#cccccc';
            saveButton.style.color = isDarkMode ? '#777777' : '#888888';
            saveButton.style.border = `2px solid ${isDarkMode ? '#555555' : '#bbbbbb'}`;
        } else {
            // Enabled styling
            saveButton.style.backgroundColor = '#4CAF50';
            saveButton.style.color = 'white';
            saveButton.style.border = 'none';
        }
    }

    // Update Revert button - always enable if content changed, regardless of errors
    if (revertButton) {
        // First set the disabled property
        const shouldDisableRevert = !hasChanged;
        revertButton.disabled = shouldDisableRevert;
        revertButton.title = hasChanged ? 'Discard changes and reload from disk' : 'No changes to revert';

        // Apply styling - ensure disabled style gets applied correctly
        if (shouldDisableRevert) {
            // Disabled styling
            revertButton.style.backgroundColor = isDarkMode ? '#444444' : '#cccccc';
            revertButton.style.color = isDarkMode ? '#777777' : '#888888';
            revertButton.style.border = `2px solid ${isDarkMode ? '#555555' : '#bbbbbb'}`;
        } else {
            // Enabled styling
            revertButton.style.backgroundColor = '#4CAF50';
            revertButton.style.color = 'white';
            revertButton.style.border = 'none';
        }
    }
}

// Custom YAML linter using js-yaml
CodeMirror.registerHelper("lint", "yaml", function(text) {
    const found = [];
    if (!text.trim()) {
        return found; // Return empty array for empty text to avoid false errors
    }

    try {
        jsyaml.load(text);
    } catch (e) {
        // Convert js-yaml error to CodeMirror lint format
        const line = e.mark && e.mark.line ? e.mark.line : 0;
        const ch = e.mark && e.mark.column ? e.mark.column : 0;
        found.push({
            from: CodeMirror.Pos(line, ch),
            to: CodeMirror.Pos(line, ch + 1),
            message: e.message,
            severity: "error"
        });
    }

    // Return the array of found issues
    return found;
});

// Initialize CodeMirror and handle dark mode
function initializeCodeMirror() {
    const textarea = document.getElementById('appsContent');

    if (!textarea) return;

    const isDarkMode = document.body.classList.contains('dark-mode');

    // Check if we have unsaved content in localStorage
    const savedContent = localStorage.getItem('appsYamlContent');

    // Store the original content for change comparison
    window.originalContent = textarea.value;

    // If we have saved content and the textarea is empty or the saved content differs from current
    if (savedContent && (!textarea.value.trim() || savedContent !== textarea.value)) {
        // Always load the saved content automatically
        textarea.value = savedContent;
        console.log('Automatically restored content from localStorage');
    }

    // Create CodeMirror instance
    editor = CodeMirror.fromTextArea(textarea, {
        mode: 'yaml',
        theme: isDarkMode ? 'monokai' : 'default', // Use default theme for light mode (pure white background)
        lineNumbers: true,
        indentUnit: 2,
        smartIndent: true,
        tabSize: 2,
        indentWithTabs: false,
        lineWrapping: false,
        gutters: ['CodeMirror-linenumbers', 'CodeMirror-lint-markers'],
        lint: {
            getAnnotations: CodeMirror.helpers.lint.yaml,
            lintOnChange: true,
            delay: 300 // Reduced delay for faster feedback
        },
        autofocus: true,
        extraKeys: {
            'Tab': function(cm) {
                if (cm.somethingSelected()) {
                    cm.indentSelection('add');
                } else {
                    cm.replaceSelection('  ', 'end', '+input');
                }
            },
            'Ctrl-Space': 'autocomplete'
        }
    });

    // Manually validate YAML when editor is ready
    editor.on('change', function() {
        // Get the content and buttons
        const content = editor.getValue();
        const saveButton = document.getElementById('saveButton');
        const revertButton = document.getElementById('revertButton');
        let isValid = true;

        try {
            // Parse YAML to check for errors
            if (content.trim()) {
                jsyaml.load(content);
            }
        } catch (e) {
            isValid = false;
            console.log('YAML validation error in change handler:', e.message);
        }

        // Update button states based on YAML validation result
        updateButtonStates(saveButton, revertButton, content, !isValid);
        console.log('Button states updated by change handler, YAML valid:', isValid);

        // Save content to localStorage whenever it changes
        localStorage.setItem('appsYamlContent', content);
        console.log('Content saved to localStorage');
    });

    // Make CodeMirror fill the available space
    editor.setSize('100%', '100%');

    // Add custom CSS to make CodeMirror fill its container properly
    const cmElement = editor.getWrapperElement();
    cmElement.style.flex = '1';
    cmElement.style.minHeight = '0';
    cmElement.style.height = 'auto';

    // Set up the lint status display
    const lintStatusEl = document.getElementById('lintStatus');
    if (lintStatusEl) {
        // Update lint status when linting is done
        editor.on('lint', (errors) => {
            console.log('Lint event fired:', errors ? errors.length : 0, 'errors found');
            const saveButton = document.getElementById('saveButton');

            // Make a direct validation attempt as backup
            let isValid = true;
            try {
                const content = editor.getValue();
                if (content && content.trim()) {
                    jsyaml.load(content);
                }
            } catch (e) {
                console.log('Manual YAML validation error during lint event:', e.message);
                isValid = false;
            }

            const content = editor.getValue();
            const revertButton = document.getElementById('revertButton');
            const hasErrors = (errors && errors.length > 0) || !isValid;

            if (hasErrors) {
                lintStatusEl.innerHTML = `<div style="color: #d32f2f; padding: 5px; border-radius: 4px; background-color: ${isDarkMode ? '#3f1e1e' : '#fff0f0'}; border: 1px solid #d32f2f;">
                    <strong>⚠️ Found ${errors ? errors.length : 'syntax'} YAML ${errors && errors.length === 1 ? 'error' : 'errors'}</strong>
                    <p style="margin: 5px 0 0 0; font-size: 14px;">Hover over the red markers in the editor gutter to see details.</p>
                </div>`;

                // Update button states with error flag
                updateButtonStates(saveButton, revertButton, content, true);
                console.log('Button states updated by lint event (with errors)');
            } else {
                // Clear the lint status when syntax is valid
                lintStatusEl.innerHTML = '';

                // Update button states with no error flag
                updateButtonStates(saveButton, revertButton, content, false);
                console.log('Button states updated by lint event (no errors)');
            }
        });

        // Initial lint after a short delay to ensure editor is fully loaded
        setTimeout(() => {
            // Perform the lint
            editor.performLint();

            // Manually check and enable the button if there are no errors
            // This is a fallback in case the lint event doesn't fire correctly
            setTimeout(() => {
                const saveButton = document.getElementById('saveButton');
                const revertButton = document.getElementById('revertButton');
                try {
                    const content = editor.getValue();
                    let isValidYaml = true;

                    // Only validate if we have content
                    if (content && content.trim()) {
                        try {
                            jsyaml.load(content);
                        } catch (e) {
                            isValidYaml = false;
                            console.log('YAML validation error in initialization:', e.message);
                        }

                        // Update button states based on content validity
                        updateButtonStates(saveButton, revertButton, content, !isValidYaml);
                        console.log('Button states updated by initial validation');

                        // Also clear the lint status if it exists and YAML is valid
                        if (lintStatusEl && isValidYaml) {
                            lintStatusEl.innerHTML = '';
                        }
                    } else {
                        // Empty content is considered valid
                        updateButtonStates(saveButton, revertButton, content, false);
                        console.log('Button states updated for empty content');
                    }
                } catch (e) {
                    // Something went wrong, keep the save button disabled but enable revert if changed
                    console.log('Error during initialization button state update:', e.message);

                    const content = editor.getValue();
                    updateButtonStates(saveButton, revertButton, content, true);
                }
            }, 300);
        }, 800);
    }

    // Apply dark mode if needed
    if (isDarkMode) {
        // Make sure the CodeMirror editor has proper dark mode styling
        const cmElement = editor.getWrapperElement();
        cmElement.style.backgroundColor = '#2d2d2d';

        // Style the gutters
        const gutters = document.querySelectorAll('.CodeMirror-gutters');
        gutters.forEach(gutter => {
            gutter.style.backgroundColor = '#2d2d2d';
            gutter.style.borderRight = '1px solid #444';
        });

        // Force a refresh to ensure all styles are applied properly
        editor.refresh();
    }
}

// Handle page load
document.addEventListener('DOMContentLoaded', function() {
    const textarea = document.getElementById('appsContent');
    if (textarea && textarea.value.trim() === '') {
        textarea.placeholder = 'apps.yaml content could not be loaded';
    }

    // Initialize CodeMirror
    initializeCodeMirror();

    // Handle Revert button click
    document.getElementById('revertButton').addEventListener('click', function() {
        if (confirm('This will discard all your unsaved changes and reload the file from disk. Are you sure?')) {
            // Remove saved content from localStorage
            localStorage.removeItem('appsYamlContent');

            // Reload the page to get fresh content from disk
            window.location.reload();
        }
    });

    // Add a direct listener to ensure the button gets enabled
    // This is a final fallback in case other methods fail
    setTimeout(() => {
        const saveButton = document.getElementById('saveButton');
        const revertButton = document.getElementById('revertButton');

        if (editor) {
            // Force a final validation check
            try {
                const content = editor.getValue();
                const hasChanged = content !== window.originalContent;

                // Check YAML validity
                let isValid = true;
                if (content && content.trim()) {
                    try {
                        jsyaml.load(content);
                    } catch (e) {
                        isValid = false;
                        console.log('YAML validation error in DOMContentLoaded final check:', e.message);
                    }
                }

                // Update buttons states consistently
                updateButtonStates(saveButton, revertButton, content, !isValid);
                console.log('Button states updated by DOMContentLoaded final check, YAML valid:', isValid);

            } catch (e) {
                console.log('YAML validation error in DOMContentLoaded:', e.message);
                // We already know there's an error, but we won't disable the button here
                // as that should be handled by the lint event
            }

            // Manual override for debugging: add a global function to force-enable the button
            window.enableSaveButton = function() {
                const saveButton = document.getElementById('saveButton');
                const revertButton = document.getElementById('revertButton');
                if (saveButton) {
                    // Force enable the save button for debugging purposes by treating content as changed and valid
                    const content = editor ? editor.getValue() : '';
                    updateButtonStates(saveButton, revertButton, content, false);
                }
            };
        }
    }, 2000); // Wait longer for everything to initialize

    // Add a listener for dark mode toggle
    window.addEventListener('storage', function(e) {
        if (e.key === 'darkMode') {
            if (editor) {
                const isDarkMode = localStorage.getItem('darkMode') === 'true';
                editor.setOption('theme', isDarkMode ? 'monokai' : 'default');

                // Update the editor's wrapper element styling
                const cmElement = editor.getWrapperElement();

                if (isDarkMode) {
                    cmElement.style.backgroundColor = '#2d2d2d';

                    // Style the gutters
                    const gutters = document.querySelectorAll('.CodeMirror-gutters');
                    gutters.forEach(gutter => {
                        gutter.style.backgroundColor = '#2d2d2d';
                        gutter.style.borderRight = '1px solid #444';
                    });

                    // Re-style any lint tooltips that might be open
                    const tooltips = document.querySelectorAll('.CodeMirror-lint-tooltip');
                    tooltips.forEach(tooltip => {
                        tooltip.style.backgroundColor = '#2d2d2d';
                        tooltip.style.color = '#f0f0f0';
                        tooltip.style.borderColor = '#444';
                    });
                } else {
                    cmElement.style.backgroundColor = '#ffffff';

                    // Style the gutters
                    const gutters = document.querySelectorAll('.CodeMirror-gutters');
                    gutters.forEach(gutter => {
                        gutter.style.backgroundColor = '#f8f8f8';
                        gutter.style.borderRight = '1px solid #ddd';
                    });

                    // Re-style any lint tooltips that might be open
                    const tooltips = document.querySelectorAll('.CodeMirror-lint-tooltip');
                    tooltips.forEach(tooltip => {
                        tooltip.style.backgroundColor = '#ffffff';
                        tooltip.style.color = '#333';
                        tooltip.style.borderColor = '#ccc';
                    });
                }

                // Re-run the linter
                editor.performLint();

                // Force a refresh to ensure all styles are applied properly
                editor.refresh();
            }
        }
    });
});
</script>
"""
    return text


def get_plan_css():
    text = """<body>
    <style>
    .dropdown {
        position: relative;
        display: inline-block;
    }

    .dropdown-content {
        display: none;
        position: absolute;
        background-color: #f9f9f9;
        min-width: 160px;
        box-shadow: 0px 8px 16px 0px rgba(0,0,0,0.2);
        z-index: 1;
        border-radius: 4px;
    }

    .dropdown-content a {
        color: black;
        padding: 12px 16px;
        text-decoration: none;
        display: block;
        cursor: pointer;
    }

    .dropdown-content a:hover {
        background-color: #f1f1f1;
    }

    .clickable-time-cell {
        cursor: pointer;
        position: relative;
        transition: background-color 0.2s;
    }

    .clickable-time-cell:hover {
        background-color: #f5f5f5 !important;
    }

    /* Dark mode styles */
    body.dark-mode .dropdown-content {
        background-color: #333;
        box-shadow: 0px 8px 16px 0px rgba(0,0,0,0.5);
    }

    body.dark-mode .dropdown-content a {
        color: #e0e0e0;
    }

    body.dark-mode .dropdown-content a:hover {
        background-color: #444;
    }

    body.dark-mode .clickable-time-cell:hover {
        background-color: #444 !important;
    }

    /* ============================
   Override cell styling
   ============================ */

    /* Generic fallback */
    .override-active {
        position: relative;
        background-color: #FFC0CB !important; /* Light pink */
    }
    body.dark-mode .override-active {
        background-color: #93264c !important; /* Dark pink */
    }

    /* Manual Charge */
    .override-charge {
        position: relative;
        background-color: #3AEE85 !important; /* Green (same as charging) */
    }
    body.dark-mode .override-charge {
        background-color: #247e59 !important;
    }

    /* Manual Export */
    .override-export {
        position: relative;
        background-color: #FFFF00 !important; /* Bright yellow */
    }
    body.dark-mode .override-export {
        background-color: #999900 !important;
    }

    /* Manual Demand */
    .override-demand {
        position: relative;
        background-color: #F18261 !important; /* Red-orange (high demand) */
    }
    body.dark-mode .override-demand {
        background-color: #7e2e1f !important;
    }

    /* Manual Freeze Charge (improved visibility) */
    .override-freeze-charge {
        position: relative;
        background-color: #C0C0C0 !important; /* Medium gray for light mode */
    }
    body.dark-mode .override-freeze-charge {
        background-color: #888888 !important; /* Lighter gray for dark mode */
    }

    /* Manual Freeze Export */
    .override-freeze-export {
        position: relative;
        background-color: #AAAAAA !important; /* Darker gray */
    }
    body.dark-mode .override-freeze-export {
        background-color: #444444 !important;
    }

    /* Rate input field styles */
    .dropdown-content input[type="number"] {
        background-color: #fff;
        color: #333;
        border: 1px solid #ccc;
    }

    .dropdown-content input[type="number"]:focus {
        outline: none;
        border-color: #4CAF50;
    }

    .dropdown-content button {
        background-color: #4CAF50;
        color: white;
        border: none;
        cursor: pointer;
    }

    .dropdown-content button:hover {
        background-color: #45a049;
    }

    /* Dark mode styles for input and button */
    body.dark-mode .dropdown-content input[type="number"] {
        background-color: #444;
        color: #e0e0e0;
        border-color: #666;
    }

    body.dark-mode .dropdown-content input[type="number"]:focus {
        border-color: #4CAF50;
    }

    body.dark-mode .dropdown-content button {
        background-color: #4CAF50;
        color: white !important;
    }

    body.dark-mode .dropdown-content button:hover {
        background-color: #45a049;
        color: white !important;
    }

    /* Dark mode styles for labels and text in dropdown */
    body.dark-mode .dropdown-content label {
        color: #e0e0e0 !important;
    }

    body.dark-mode .dropdown-content div {
        color: #e0e0e0 !important;
    }
    </style>

    <script>
    // Close all dropdown menus
    function closeDropdowns() {
        var dropdowns = document.getElementsByClassName("dropdown-content");
        for (var i = 0; i < dropdowns.length; i++) {
            if (dropdowns[i].style.display === "block") {
                dropdowns[i].style.display = "none";
            }
        }
    }

    // Toggle dropdown menu
    function toggleForceDropdown(id) {
        closeDropdowns();
        var dropdown = document.getElementById(id);
        if (dropdown.style.display === "block") {
            dropdown.style.display = "none";
        } else {
            dropdown.style.display = "block";
        }
    }

    // Handle rate override option function
    function handleRateOverride(time, rate, action, clear) {
        console.log("Rate override:", time, "Rate:", rate, "Action:", action);
        // Create a form data object to send the override parameters
        const formData = new FormData();
        formData.append('time', time);
        formData.append('rate', rate);
        formData.append('action', action);
        // Send the override request to the server
        fetch('./rate_override', {
            method: 'POST',
            body: formData
        })
        .then(response => {
            if (response.ok) {
                return response.json();
            }
            throw new Error('Failed to set rate override');
        })
        .then(data => {
            if (data.success) {
                // Show success message
                const messageElement = document.createElement('div');
                if (clear) {
                    messageElement.textContent = `Manual rate cleared for ${time}`;
                } else {
                    messageElement.textContent = `Rate set to ${rate}p/kWh for ${time}`;
                }
                messageElement.style.position = 'fixed';
                messageElement.style.top = '65px';
                messageElement.style.right = '10px';
                messageElement.style.padding = '10px';
                messageElement.style.backgroundColor = '#4CAF50';
                messageElement.style.color = 'white';
                messageElement.style.borderRadius = '4px';
                messageElement.style.zIndex = '1000';
                document.body.appendChild(messageElement);

                // Auto-remove message after 3 seconds
                setTimeout(() => {
                    messageElement.style.opacity = '0';
                    messageElement.style.transition = 'opacity 0.5s';
                    setTimeout(() => messageElement.remove(), 500);
                }, 3000);

                // Reload the page to show the updated plan
                setTimeout(() => location.reload(), 1000);
            } else {
                alert('Error setting rate override: ' + (data.message || 'Unknown error'));
            }
        })
        .catch(error => {
            console.error('Error:', error);
            alert('Error setting rate override: ' + (error.message || 'Unknown error'));
        });
        // Close dropdown after selection
        closeDropdowns();

    }

    // Handle rate override option function
    function handleLoadOverride(time, adjustment, action, clear) {
        console.log("Load override:", time, "Adjustment:", adjustment, "Action:", action);
        // Create a form data object to send the override parameters
        const formData = new FormData();
        formData.append('time', time);
        formData.append('rate', adjustment);
        formData.append('action', action);
        // Send the override request to the server
        fetch('./rate_override', {
            method: 'POST',
            body: formData
        })
        .then(response => {
            if (response.ok) {
                return response.json();
            }
            throw new Error('Failed to set rate override');
        })
        .then(data => {
            if (data.success) {
                // Show success message
                const messageElement = document.createElement('div');
                if (clear) {
                    messageElement.textContent = `Manual load adjustment cleared for ${time}`;
                } else {
                    messageElement.textContent = `Load adjustment set to ${adjustment} for ${time}`;
                }
                messageElement.style.position = 'fixed';
                messageElement.style.top = '65px';
                messageElement.style.right = '10px';
                messageElement.style.padding = '10px';
                messageElement.style.backgroundColor = '#4CAF50';
                messageElement.style.color = 'white';
                messageElement.style.borderRadius = '4px';
                messageElement.style.zIndex = '1000';
                document.body.appendChild(messageElement);

                // Auto-remove message after 3 seconds
                setTimeout(() => {
                    messageElement.style.opacity = '0';
                    messageElement.style.transition = 'opacity 0.5s';
                    setTimeout(() => messageElement.remove(), 500);
                }, 3000);

                // Reload the page to show the updated plan
                setTimeout(() => location.reload(), 1000);
            } else {
                alert('Error setting load adjustment override: ' + (data.message || 'Unknown error'));
            }
        })
        .catch(error => {
            console.error('Error:', error);
            alert('Error setting load adjustment override: ' + (error.message || 'Unknown error'));
        });
        // Close dropdown after selection
        closeDropdowns();

    }


    // Handle option selection
    function handleTimeOverride(time, action) {
        console.log("Time override:", time, "Action:", action);

        // Create a form data object to send the override parameters
        const formData = new FormData();
        formData.append('time', time);
        formData.append('action', action);

        // Send the override request to the server
        fetch('./plan_override', {
            method: 'POST',
            body: formData
        })
        .then(response => {
            if (response.ok) {
                return response.json();
            }
            throw new Error('Failed to set plan override');
        })
        .then(data => {
            if (data.success) {
                // Show success message
                const messageElement = document.createElement('div');
                messageElement.textContent = `${action} override set for ${time}`;
                messageElement.style.position = 'fixed';
                messageElement.style.top = '65px';
                messageElement.style.right = '10px';
                messageElement.style.padding = '10px';
                messageElement.style.backgroundColor = '#4CAF50';
                messageElement.style.color = 'white';
                messageElement.style.borderRadius = '4px';
                messageElement.style.zIndex = '1000';
                document.body.appendChild(messageElement);

                // Auto-remove message after 3 seconds
                setTimeout(() => {
                    messageElement.style.opacity = '0';
                    messageElement.style.transition = 'opacity 0.5s';
                    setTimeout(() => messageElement.remove(), 500);
                }, 3000);

                // Reload the page to show the updated plan
                setTimeout(() => location.reload(), 1000);
            } else {
                alert('Error setting override: ' + (data.message || 'Unknown error'));
            }
        })
        .catch(error => {
            console.error('Error:', error);
            alert('Error setting override: ' + error.message);
        });

        // Close dropdown after selection
        closeDropdowns();
    }

    // Close dropdowns when clicking outside
    document.addEventListener("click", function(event) {
        if (!event.target.matches('.clickable-time-cell') && !event.target.closest('.dropdown-content')) {
            closeDropdowns();
        }
    });
    </script>
    """
    return text


def get_header_html(title, calculating, default_page, arg_errors, THIS_VERSION, battery_status_icon, refresh=0, codemirror=False):
    """
    Return the HTML header for a page
    """

    text = '<!doctype html><html><head><meta charset="utf-8"><title>{}</title>'.format(title)

    text += """
<link href="https://cdn.jsdelivr.net/npm/@mdi/font@7.4.47/css/materialdesignicons.min.css" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/apexcharts"></script>
<style>
    body, html {
        margin: 0;
        padding: 0;
        height: 100%;
        border: 2px solid #ffffff;
    }
    body {
        font-family: Arial, sans-serif;
        text-align: left;
        margin: 5px;
        background-color: #ffffff;
        color: #333;
    }
    h1 {
        color: #4CAF50;
    }
    h2 {
        color: #4CAF50;
        display: inline
    }
    p {
        white-space: nowrap;
    }
    table {
        padding: 1px;
        border: 2px solid green;
        border-spacing: 2px;
        background-clip: padding-box;
    }
    th,
    td {
        text-align: left;
        padding: 5px;
        vertical-align: top;
    }
    th {
        background-color: #4CAF50;
        color: white;
    }
    .default, .cfg_default {
        background-color: #ffffff;
    }
    .modified, .cfg_modified {
        background-color: #ffcccc;
    }

    /* Apply dark mode to html element as well for earlier styling */
    html.dark-mode,
    body.dark-mode {
        background-color: #121212;
        color: #e0e0e0;
        border: 2px solid #121212;
    }
    body.dark-mode table {
        border-color: #333;
    }
    body.dark-mode th {
        background-color: #333;
        color: #e0e0e0;
    }
    body.dark-mode .default,
    body.dark-mode .cfg_default {
        background-color: #121212;
    }
    body.dark-mode .modified,
    body.dark-mode .cfg_modified {
        background-color: #662222;
    }
    /* Dark mode link styles */
    body.dark-mode a {
        color: #8cb4ff;
    }
    body.dark-mode a:visited {
        color: #c58cff;
    }
    body.dark-mode a:hover {
        color: #afd2ff;
    }
    /* Dark mode chart styles */
    body.dark-mode .apexcharts-legend-text {
        color: #ffffff !important;
    }
    body.dark-mode .apexcharts-title-text {
        fill: #ffffff !important;
    }
    body.dark-mode .apexcharts-xaxis-label,
    body.dark-mode .apexcharts-yaxis-label {
        fill: #e0e0e0 !important;
    }
    /* Dark mode tooltip styles */
    body.dark-mode .apexcharts-tooltip {
        background: #333 !important;
        color: #e0e0e0 !important;
        border: 1px solid #555 !important;
    }
    body.dark-mode .apexcharts-tooltip-title {
        background: #444 !important;
        color: #e0e0e0 !important;
        border-bottom: 1px solid #555 !important;
    }
    body.dark-mode .apexcharts-tooltip-series-group {
        border-bottom: 1px solid #555 !important;
    }
    body.dark-mode .apexcharts-tooltip-text {
        color: #e0e0e0 !important;
    }
    body.dark-mode .apexcharts-tooltip-text-y-value,
    body.dark-mode .apexcharts-tooltip-text-goals-value,
    body.dark-mode .apexcharts-tooltip-text-z-value {
        color: #e0e0e0 !important;
    }
    body.dark-mode .apexcharts-tooltip-marker {
        box-shadow: 0 0 0 1px #444 !important;
    }
    body.dark-mode .apexcharts-tooltip-text-label {
        color: #afd2ff !important;
    }
    body.dark-mode .apexcharts-xaxistooltip,
    body.dark-mode .apexcharts-yaxistooltip {
        background: #333 !important;
        color: #e0e0e0 !important;
        border: 1px solid #555 !important;
    }
    body.dark-mode .apexcharts-xaxistooltip-text,
    body.dark-mode .apexcharts-yaxistooltip-text {
        color: #e0e0e0 !important;
    }

    /* Dark mode styles for restart button */
    body.dark-mode button {
        background-color: #cc3333 !important;
        color: #e0e0e0 !important;
    }

    body.dark-mode button:hover {
        background-color: #aa2222 !important;
    }

    /* Dashboard form controls styling */
    .dashboard-select {
        border: 1px solid #ccc;
        padding: 2px 4px;
        border-radius: 3px;
        background-color: #fff;
        color: #333;
        font-size: 14px;
    }

    /* Toggle switch styles */
    .toggle-switch {
        position: relative;
        display: inline-block;
        width: 60px;
        height: 24px;
        background-color: #ccc;
        border-radius: 12px;
        cursor: pointer;
        transition: background-color 0.3s;
        border: none;
        outline: none;
        margin-right: 5px;
        vertical-align: middle;
    }

    .toggle-switch.active {
        background-color: #f44336;
    }

    .toggle-switch::before {
        content: '';
        position: absolute;
        top: 2px;
        left: 2px;
        width: 20px;
        height: 20px;
        background-color: white;
        border-radius: 50%;
        transition: transform 0.3s;
        box-shadow: 0 2px 4px rgba(0,0,0,0.2);
    }

    .toggle-switch.active::before {
        transform: translateX(36px);
    }

    .toggle-switch:hover {
        opacity: 0.8;
    }

    /* Dark mode form controls */
    body.dark-mode .dashboard-select {
        background-color: #444;
        color: #e0e0e0;
        border-color: #666;
    }

    body.dark-mode .dashboard-select:focus {
        border-color: #4CAF50;
    }

    /* Dark mode toggle switch styles */
    body.dark-mode .toggle-switch {
        background-color: #555 !important;
    }

    body.dark-mode .toggle-switch.active {
        background-color: #f44336 !important;
    }

    body.dark-mode .toggle-switch::before {
        background-color: #e0e0e0;
    }

    .menu-bar {
        background-color: #ffffff;
        overflow-x: auto; /* Enable horizontal scrolling */
        white-space: nowrap; /* Prevent menu items from wrapping */
        display: flex;
        align-items: center;
        border-bottom: 1px solid #ddd;
        -webkit-overflow-scrolling: touch; /* Smooth scrolling on iOS */
        scrollbar-width: thin; /* Firefox */
        scrollbar-color: #4CAF50 #f0f0f0; /* Firefox */
        position: fixed; /* Change from sticky to fixed */
        top: 0; /* Stick to the top */
        left: 0; /* Ensure it starts from the left edge */
        right: 0; /* Ensure it extends to the right edge */
        width: 100%; /* Make sure it spans the full width */
        z-index: 1000; /* Ensure it's above other content */
        box-shadow: 0 2px 5px rgba(0, 0, 0, 0.1); /* Add subtle shadow for visual separation */
    }

    /* Add padding to body to prevent content from hiding under fixed header */
    body {
        padding-top: 65px; /* Increased padding to account for the fixed menu height */
    }

    .battery-wrapper {
        display: flex;
        align-items: center;
        margin-left: 10px;
    }

    /* Flying bat animation */
    @keyframes flyAcross {
        0% {
            left: 10px;
            top: 30px;
            transform: translateY(0) scale(1.5);
        }
        25% {
            left: 25%;
            top: 65%;
            transform: translateY(0) scale(2.0) rotate(45deg);
        }
        50% {
            left: 50%;
            top: 30px;
            transform: translateY(0) scale(1.5) rotate(-45deg);
        }
        75% {
            left: 75%;
            top: 65%;
            transform: translateY(0) scale(2.0) rotate(45deg);
        }
        100% {
            left: 100%;
            top: 30px;
            transform: translateY(0) scale(1.5);
        }
    }

    .flying-bat {
        position: fixed;
        z-index: 9999;
        width: 60px;
        height: 60px;
        background-size: contain;
        background-repeat: no-repeat;
        background-position: center;
        pointer-events: none;
        animation: flyAcross 3s linear forwards;
    }

</style>
<script>
// Check and apply the saved dark mode preference on page load
window.onload = function() {
    applyDarkMode();
};
function applyDarkMode() {
    const darkModeEnabled = localStorage.getItem('darkMode') === 'true';
    if (darkModeEnabled) {
        document.body.classList.add('dark-mode');
        document.documentElement.classList.add('dark-mode');
    }
    else {
        document.body.classList.remove('dark-mode');
        document.documentElement.classList.remove('dark-mode');
    }

    // Update logo image source based on dark mode
    const logoImage = document.getElementById('logo-image');
    if (logoImage) {
        if (darkModeEnabled) {
            logoImage.src = logoImage.getAttribute('data-dark-src');
        } else {
            logoImage.src = logoImage.getAttribute('data-light-src');
        }
    }
};

function toggleDarkMode() {
    const isDarkMode = document.body.classList.toggle('dark-mode');
    localStorage.setItem('darkMode', isDarkMode);
    // Force reload to apply dark mode styles
    location.reload();
}

function flyBat() {
    // Remove any existing flying bats
    document.querySelectorAll('.flying-bat').forEach(bat => bat.remove());

    // Create a new bat element
    const bat = document.createElement('div');
    bat.className = 'flying-bat';

    // Get the appropriate bat image based on dark/light mode
    const isDarkMode = document.body.classList.contains('dark-mode');
    const batImage = isDarkMode
        ? 'https://raw.githubusercontent.com/springfall2008/batpred/refs/heads/main/docs/images/bat_logo_dark.png'
        : 'https://raw.githubusercontent.com/springfall2008/batpred/refs/heads/main/docs/images/bat_logo_light.png';

    bat.style.backgroundImage = `url('${batImage}')`;

    // Add to document
    document.body.appendChild(bat);

    // Remove after animation completes
    setTimeout(() => {
        bat.remove();
    }, 4100);  // Slightly longer than the animation duration
}

function restartPredbat() {
    if (confirm('Are you sure you want to restart Predbat?')) {
        fetch('./restart', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            }
        })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                alert('Restart initiated. Predbat will restart shortly.');
                // Reload the page after a short delay to show the restart status
                setTimeout(() => {
                    window.location.reload();
                }, 2000);
            } else {
                alert('Error initiating restart: ' + (data.message || 'Unknown error'));
            }
        })
        .catch(error => {
            console.error('Error:', error);
            alert('Error initiating restart: ' + error.message);
        });
    }
}

function toggleSwitch(element, fieldName) {
    // Toggle the active class
    element.classList.toggle('active');

    // Determine the new value
    const isActive = element.classList.contains('active');
    const newValue = isActive ? 'on' : 'off';

    // Find the associated form and hidden input
    const form = element.closest('form');
    if (form) {
        // Create or update the hidden input for the value
        let hiddenInput = form.querySelector('input[name="' + fieldName + '"]');
        if (!hiddenInput) {
            hiddenInput = document.createElement('input');
            hiddenInput.type = 'hidden';
            hiddenInput.name = fieldName;
            form.appendChild(hiddenInput);
        }
        hiddenInput.value = newValue;

        // If saveFilterValue function exists (config page), call it
        if (typeof saveFilterValue === 'function') {
            saveFilterValue();
        }

        // Submit the form
        form.submit();
    }
}
</script>
"""
    if refresh:
        text += '<meta http-equiv="refresh" content="{}" >'.format(refresh)

    if codemirror:
        text += """
    <!-- CodeMirror Library -->
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.9/codemirror.min.css">
    <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.9/codemirror.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.9/mode/yaml/yaml.min.js"></script>

    <!-- CodeMirror Theme -->
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.9/theme/monokai.min.css">

    <!-- YAML Validation and Linting -->
    <script src="https://cdnjs.cloudflare.com/ajax/libs/js-yaml/4.1.0/js-yaml.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.9/addon/lint/lint.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.9/addon/lint/yaml-lint.js"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.9/addon/lint/lint.min.css">
    </head>"""
    text += "</head><body>"
    text += get_menu_html(calculating, default_page, arg_errors, THIS_VERSION, battery_status_icon)
    return text


def get_menu_html(calculating, default_page, arg_errors, THIS_VERSION, battery_status_icon):
    """
    Return the Predbat Menu page as HTML
    """
    text = ""
    # Check if there are configuration errors
    config_warning = ""
    if arg_errors:
        config_warning = '<span style="color: #ffcc00; margin-left: 5px;">&#9888;</span>'

    # Define status icon based on calculating state
    status_icon = ""
    if calculating:
        status_icon = '<span class="mdi mdi-sync mdi-spin calculating-icon" style="color: #4CAF50; font-size: 24px; margin-left: 10px; margin-right: 10px;" title="Calculation in progress..."></span>'
    else:
        status_icon = '<span class="mdi mdi-check-circle idle-icon" style="color: #4CAF50; font-size: 24px; margin-left: 10px; margin-right: 10px;" title="System idle"></span>'

    text += (
        """
<style>
.menu-bar {
background-color: #ffffff;
overflow-x: auto; /* Enable horizontal scrolling */
white-space: nowrap; /* Prevent menu items from wrapping */
display: flex;
align-items: center;
border-bottom: 1px solid #ddd;
-webkit-overflow-scrolling: touch; /* Smooth scrolling on iOS */
scrollbar-width: thin; /* Firefox */
scrollbar-color: #4CAF50 #f0f0f0; /* Firefox */
position: fixed; /* Change from sticky to fixed */
top: 0; /* Stick to the top */
left: 0; /* Ensure it starts from the left edge */
right: 0; /* Ensure it extends to the right edge */
width: 100%; /* Make sure it spans the full width */
z-index: 1000; /* Ensure it's above other content */
box-shadow: 0 2px 5px rgba(0, 0, 0, 0.1); /* Add subtle shadow for visual separation */
}

/* Add padding to body to prevent content from hiding under fixed header */
body {
padding-top: 65px; /* Increased padding to account for the fixed menu height */
}

.menu-bar .logo {
display: flex;
align-items: center;
padding: 0 16px;
min-width: fit-content; /* Prevent logo from shrinking */
}

.menu-bar .logo img {
height: 40px;
margin-right: 10px;
}

.menu-bar .logo-text {
font-size: 24px;
font-weight: bold;
color: #333;
white-space: nowrap;
}

.menu-bar a {
color: #333;
text-align: center;
padding: 14px 16px;
text-decoration: none;
font-size: 16px;
display: flex;
align-items: center;
white-space: nowrap;
flex-shrink: 0; /* Prevent items from shrinking */
}



.menu-bar a:hover {
background-color: #f0f0f0;
color: #4CAF50;
}

.menu-bar a.active {
background-color: #4CAF50;
color: white;
}

.dark-mode-toggle {
margin-left: auto;
padding: 14px 16px;
flex-shrink: 0; /* Prevent from shrinking */
}

.dark-mode-toggle button {
background-color: #f0f0f0;
color: #333;
border: 1px solid #ddd;
padding: 8px 12px;
border-radius: 4px;
cursor: pointer;
white-space: nowrap;
}

.dark-mode-toggle button:hover {
background-color: #e0e0e0;
}

/* Dark mode menu styles */
body.dark-mode .menu-bar {
background-color: #1e1e1e;
border-bottom: 1px solid #333;
scrollbar-color: #4CAF50 #333; /* Firefox */
}

body.dark-mode .menu-bar::-webkit-scrollbar-track {
background: #333;
}

body.dark-mode .menu-bar .logo-text {
color: white;
}

body.dark-mode .menu-bar a {
color: white;
}

body.dark-mode .menu-bar a:hover {
background-color: #2c652f;
color: white;
}

body.dark-mode .menu-bar a.active {
background-color: #4CAF50;
color: white;
}

body.dark-mode .calculating-icon {
color: #6CFF72 !important;
}

body.dark-mode .idle-icon {
color: #6CFF72 !important;
}

body.dark-mode .dark-mode-toggle button {
background-color: #444;
color: #e0e0e0;
border-color: #555;
}

body.dark-mode .dark-mode-toggle button:hover {
background-color: #666;
}
</style>

<script>
// Add viewport meta tag if it doesn't exist
if (!document.querySelector('meta[name="viewport"]')) {
const meta = document.createElement('meta');
meta.name = 'viewport';
meta.content = 'width=device-width, initial-scale=1, maximum-scale=1';
document.head.appendChild(meta);
}

// Store the active menu item in session storage
function storeActiveMenuItem(path) {
localStorage.setItem('activeMenuItem', path);
}

// Function to set the active menu item
function setActiveMenuItem() {
// Get all menu links
const menuLinks = document.querySelectorAll('.menu-bar a');

// Get current page path from window location
let currentPath = window.location.pathname;

// Handle paths with trailing slash
if (currentPath.endsWith('/')) {
    currentPath = currentPath.slice(0, -1);
}

// Default page from server if nothing else matches
const defaultPage = '"""
        + default_page
        + """';

// First try to get the active page from session storage (in case of resize or direct navigation)
const storedActivePage = localStorage.getItem('activeMenuItem');

let currentPage = currentPath;

// If the current page is the root, check if we have a stored page
if (currentPath === '' || currentPath === '/') {
    if (storedActivePage) {
        currentPage = storedActivePage;
    } else {
        currentPage = defaultPage;
    }
} else {
    // Store the current page for future reference
    localStorage.setItem('activeMenuItem', currentPage);
}

let activeFound = false;

// Remove active class from all links
menuLinks.forEach(link => {
    link.classList.remove('active');

    // Check if this link's href matches the current page
    const linkPath = new URL(link.href).pathname;

    // Ensure we're comparing cleanly
    const cleanLinkPath = linkPath.endsWith('/') ? linkPath.slice(0, -1) : linkPath;
    const cleanCurrentPage = currentPage.endsWith('/') ? currentPage.slice(0, -1) : currentPage;

    // Match either the exact path or paths with a leading ./
    // (since server-side our paths often have ./ prefix)
    if (cleanCurrentPage === cleanLinkPath ||
        cleanLinkPath.endsWith(cleanCurrentPage) ||
        cleanCurrentPage.endsWith(cleanLinkPath)) {
        link.classList.add('active');
        activeFound = true;
    }
});

// If no active item was found, set default
if (!activeFound && menuLinks.length > 0) {
    const defaultLink = menuLinks[0]; // Set first menu item as default
    defaultLink.classList.add('active');
    storeActiveMenuItem(new URL(defaultLink.href).pathname);
}

// Scroll active item into view
const activeItem = document.querySelector('.menu-bar a.active');
if (activeItem) {
    // Scroll with a slight offset to make it more visible
    const menuBar = document.querySelector('.menu-bar');
    const activeItemLeft = activeItem.offsetLeft;
    const menuBarWidth = menuBar.clientWidth;
    menuBar.scrollLeft = activeItemLeft - menuBarWidth / 2 + activeItem.clientWidth / 2;
}
}

// Initialize menu on page load
document.addEventListener("DOMContentLoaded", function() {
setActiveMenuItem();

// For each menu item, add click handler to set it as active
const menuLinks = document.querySelectorAll('.menu-bar a');
menuLinks.forEach(link => {
    link.addEventListener('click', function(e) {
        // Don't override external links (like Docs)
        if (!this.href.includes(window.location.hostname)) {
            return;
        }

        // Remove active class from all links
        menuLinks.forEach(l => l.classList.remove('active'));

        // Add active class to clicked link
        this.classList.add('active');

        // Store the clicked menu item path
        storeActiveMenuItem(new URL(this.href).pathname);
    });
});
});

// Additional window.onload handler for other functionality
const originalOnLoad = window.onload;
window.onload = function() {
// Call the original onload function if it exists
if (typeof originalOnLoad === 'function') {
    originalOnLoad();
}
applyDarkMode();
};

// Handle window resize without losing active menu item
window.addEventListener('resize', function() {
// Don't reload the page, just make sure the active menu item is visible
setTimeout(function() {
    const activeItem = document.querySelector('.menu-bar a.active');
    if (activeItem) {
        const menuBar = document.querySelector('.menu-bar');
        const activeItemLeft = activeItem.offsetLeft;
        const menuBarWidth = menuBar.clientWidth;
        menuBar.scrollLeft = activeItemLeft - menuBarWidth / 2 + activeItem.clientWidth / 2;
    }
}, 100);
});
</script>

<div class="menu-bar">
<div class="logo">
    <img id="logo-image"
            src="https://raw.githubusercontent.com/springfall2008/batpred/refs/heads/main/docs/images/bat_logo_light.png"
            data-light-src="https://raw.githubusercontent.com/springfall2008/batpred/refs/heads/main/docs/images/bat_logo_light.png"
            data-dark-src="https://raw.githubusercontent.com/springfall2008/batpred/refs/heads/main/docs/images/bat_logo_dark.png"
            alt="Predbat Logo"
            onclick="flyBat()"
            style="cursor: pointer;"
    >
    """
        + status_icon
        + """
    <div class="battery-wrapper">
        """
        + battery_status_icon
        + """
    </div>
</div>
<a href='./dash'>Dash</a>
<a href='./plan'>Plan</a>
<a href='./entity'>Entities</a>
<a href='./charts'>Charts</a>
<a href='./config'>Config</a>
<a href='./apps'>Apps"""
        + config_warning
        + """</a>
<a href='./components'>Components</a>
<a href='./apps_editor'>Editor</a>
<a href='./log'>Log</a>
<a href='./compare'>Compare</a>
<a href='https://springfall2008.github.io/batpred/'>Docs</a>
<div class="dark-mode-toggle">
    """
        + THIS_VERSION
        + """
    <button onclick="toggleDarkMode()">Toggle Dark Mode</button>
</div>
</div>
"""
    )
    return text
