// Connect to your live backend API
const API_BASE = 'https://abrt-scraper-2-xbbl.onrender.com';

document.addEventListener('DOMContentLoaded', function() {
    console.log("Abrimax frontend JS loaded!");

    // 1. Get the DOM elements (Assuming your buttons/inputs have these IDs. 
    // If your HTML has different IDs, you must change the IDs below to match!)
    const loginBtn = document.getElementById('loginBtn');
    const signupBtn = document.getElementById('signupBtn');
    const switchToSignup = document.getElementById('switchToSignup');
    const switchToLogin = document.getElementById('switchToLogin');

    // 2. Handle Login
    if (loginBtn) {
        loginBtn.addEventListener('click', async function() {
            const email = document.getElementById('loginEmail').value;
            const password = document.getElementById('loginPassword').value;

            if (!email || !password) {
                alert("Please fill in all fields.");
                return;
            }

            try {
                const response = await fetch(`${API_BASE}/api/login`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ email, password })
                });

                const data = await response.json();

                if (data.token) {
                    // Save the token to browser storage so they stay logged in
                    localStorage.setItem('authToken', data.token);
                    alert("Login successful! Welcome to Abrimax.");
                    console.log("User logged in:", data);
                    // Redirect to the dashboard page here!
                    // window.location.href = 'dashboard.html'; 
                } else {
                    alert("Login failed: " + (data.error || "Unknown error"));
                }
            } catch (error) {
                console.error("Login error:", error);
                alert("Network error. Check console for details.");
            }
        });
    }

    // 3. Handle Sign Up
    if (signupBtn) {
        signupBtn.addEventListener('click', async function() {
            const email = document.getElementById('signupEmail').value;
            const phone = document.getElementById('signupPhone').value;
            const password = document.getElementById('signupPassword').value;

            if (!email || !phone || !password) {
                alert("Please fill in all fields.");
                return;
            }

            try {
                const response = await fetch(`${API_BASE}/api/signup`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ email, phone, password })
                });

                const data = await response.json();

                if (data.token) {
                    localStorage.setItem('authToken', data.token);
                    alert("Account created successfully! You are now logged in.");
                    // Redirect to the dashboard page here!
                    // window.location.href = 'dashboard.html'; 
                } else {
                    alert("Sign up failed: " + (data.error || "Unknown error"));
                }
            } catch (error) {
                console.error("Signup error:", error);
                alert("Network error. Check console for details.");
            }
        });
    }
});
