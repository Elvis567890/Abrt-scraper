// ============================================================
// 1. BASE URL
// ============================================================
const API_BASE = 'https://sms-payment-system-9.onrender.com';

// ============================================================
// 2. GET ACTIVE PLANS
// ============================================================
export async function getPlans() {
  const res = await fetch(`${API_BASE}/api/active-plans`);
  if (!res.ok) throw new Error('Failed to fetch plans');
  return res.json();
}

// ============================================================
// 3. INITIATE PAYMENT (User selects plan)
// ============================================================
export async function initiatePayment(userId, plan) {
  const res = await fetch(`${API_BASE}/api/initiate-payment`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ user_id: userId, plan })
  });
  if (!res.ok) throw new Error('Failed to initiate payment');
  return res.json();
}

// ============================================================
// 4. SUBMIT TRANSACTION ID (User enters the ID from SMS)
// ============================================================
export async function submitTransaction(userId, amount, plan, manualTransactionId) {
  const res = await fetch(`${API_BASE}/api/transactions`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      user_id: userId,
      amount,
      plan,
      manual_transaction_id: manualTransactionId
    })
  });
  if (!res.ok) throw new Error('Failed to submit transaction');
  return res.json();
}

// ============================================================
// 5. CHECK TRANSACTION STATUS
// ============================================================
export async function checkTransactionStatus(userId) {
  const res = await fetch(`${API_BASE}/api/transactions?user_id=${userId}`);
  if (!res.ok) throw new Error('Failed to fetch transactions');
  return res.json();
}

// ============================================================
// 6. CHECK SUBSCRIPTION STATUS (polling)
// ============================================================
export async function checkSubscription(userId) {
  const data = await checkTransactionStatus(userId);
  const successTx = data.transactions?.find(tx => tx.status === 'success');
  return {
    isActive: !!successTx,
    transaction: successTx || null
  };
}

// ============================================================
// 7. USER AUTHENTICATION
// ============================================================
export async function signup(email, phone, password) {
  const res = await fetch(`${API_BASE}/api/signup`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, phone, password })
  });
  if (!res.ok) throw new Error('Failed to sign up');
  return res.json();
}

export async function login(email, password) {
  const res = await fetch(`${API_BASE}/api/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password })
  });
  if (!res.ok) throw new Error('Failed to log in');
  return res.json();
    }
