#!/bin/csh -f

while (1)
    echo "Starting Predbat..."
    python3 hass.py
    echo "Predbat crashed. Restarting in 5 seconds..."
    sleep 5
end
