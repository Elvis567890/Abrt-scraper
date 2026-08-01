const express = require('express');
const cors = require('cors');
const { createClient } = require('@supabase/supabase-js');

const app = express();
app.use(cors());
app.use(express.json());

// Supabase config
const supabaseUrl = process.env.SUPABASE_URL || 'https://iaruxqgvliyfzpobmbjl.supabase.co';
const supabaseKey = process.env.SUPABASE_SERVICE_ROLE_KEY;
const supabase = createClient(supabaseUrl, supabaseKey);

// Webhook endpoint - accepts ANY format
app.post('/webhook', async (req, res) => {
  console.log('📨 RAW HEADERS:', req.headers);
  console.log('📨 RAW BODY:', JSON.stringify(req.body, null, 2));
  console.log('📨 RAW BODY TYPE:', typeof req.body);

  // Try multiple ways to extract sender and message
  let sender = req.body.sender || req.body.from || req.body.phone || req.body.number || req.body.Sender || req.body.From;
  let message = req.body.message || req.body.text || req.body.body || req.body.Message || req.body.Text || req.body.Body;

  // If body is empty, check if the request came as query params
  if (!sender && !message) {
    sender = req.query.sender || req.query.from || req.query.phone;
    message = req.query.message || req.query.text || req.query.body;
  }

  // If still empty, check raw body string
  if (!sender && !message && typeof req.body === 'string') {
    try {
      const parsed = JSON.parse(req.body);
      sender = parsed.sender || parsed.from || parsed.phone;
      message = parsed.message || parsed.text || parsed.body;
    } catch (e) {
      // Not JSON, try to parse as plain text
      message = req.body;
    }
  }

  if (!sender && !message) {
    console.error('❌ Could not extract sender or message from:', req.body);
    return res.status(400).json({ 
      error: 'Could not extract sender or message',
      received: req.body,
      headers: req.headers
    });
  }

  console.log(`📱 Sender: ${sender}`);
  console.log(`💬 Message: ${message}`);

  try {
    // Parse SMS to extract transaction details
    const parsed = parseSMS(message);
    if (!parsed) {
      console.error('❌ Unrecognized SMS format');
      return res.status(400).json({ error: 'Unrecognized SMS format', message: message });
    }

    const { transactionId, amount } = parsed;
    console.log(`🔑 Transaction ID: ${transactionId}, Amount: ${amount}`);

    // Check for duplicate transaction
    const { data: existing } = await supabase
      .from('payments')
      .select('id')
      .eq('transaction_id', transactionId)
      .maybeSingle();

    if (existing) {
      console.warn(`⚠️ Duplicate transaction: ${transactionId}`);
      return res.status(400).json({ error: 'Duplicate transaction' });
    }

    // Find pending payment
    const { data: pending } = await supabase
      .from('payments')
      .select('*')
      .eq('sender_phone', sender)
      .eq('amount', amount)
      .eq('status', 'pending')
      .order('created_at', { ascending: false })
      .limit(1);

    if (!pending || pending.length === 0) {
      console.warn(`❌ No pending payment for sender ${sender}, amount ${amount}`);
      return res.status(404).json({ error: 'No matching pending payment' });
    }

    const payment = pending[0];
    const planConfig = { day_pass: 1, monthly_vip: 30, quarterly_pro: 90 };
    const durationDays = planConfig[payment.selected_plan] || 30;
    const expiry = new Date(Date.now() + durationDays * 24 * 60 * 60 * 1000);

    // Update payment
    await supabase
      .from('payments')
      .update({
        status: 'paid',
        transaction_id: transactionId,
        sms_received_at: new Date().toISOString(),
        verified_at: new Date().toISOString()
      })
      .eq('id', payment.id);

    // Update user
    await supabase
      .from('users')
      .update({
        subscription_status: 'active',
        subscription_plan: payment.selected_plan,
        subscription_start: new Date().toISOString(),
        subscription_expiry: expiry.toISOString()
      })
      .eq('id', payment.user_id);

    console.log(`✅ Activated for user: ${payment.user_id}`);
    res.json({ success: true, userId: payment.user_id, transactionId });

  } catch (error) {
    console.error('❌ Webhook error:', error);
    res.status(500).json({ error: 'Internal server error' });
  }
});

// Health check
app.get('/', (req, res) => {
  res.send('SMS Webhook is running!');
});

// SMS Parser
function parseSMS(message) {
  const patterns = [
    /received UGX ([\d,]+) from (\d+).*Ref: ([A-Z0-9]+)/i,
    /received ([\d,]+) UGX from (\d+).*Ref: ([A-Z0-9]+)/i,
    /Ref: ([A-Z0-9]+).*received UGX ([\d,]+) from (\d+)/i,
    /Ref: ([A-Z0-9]+).*received ([\d,]+) UGX from (\d+)/i
  ];

  for (const pattern of patterns) {
    const match = message.match(pattern);
    if (match) {
      let amount, senderNumber, transactionId;
      if (pattern.source.includes('Ref: ([A-Z0-9]+).*received')) {
        transactionId = match[1];
        amount = parseFloat(match[2].replace(/,/g, ''));
        senderNumber = match[3];
      } else {
        amount = parseFloat(match[1].replace(/,/g, ''));
        senderNumber = match[2];
        transactionId = match[3];
      }
      return { transactionId, amount, senderNumber };
    }
  }
  return null;
}

const port = process.env.PORT || 3000;
app.listen(port, () => {
  console.log(`🚀 Server running on port ${port}`);
});
