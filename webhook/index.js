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

// Webhook endpoint
app.post('/webhook', async (req, res) => {
  console.log('📨 Full request body:', JSON.stringify(req.body, null, 2));

  // Support both formats: sender/message OR from/text
  const sender = req.body.sender || req.body.from;
  const message = req.body.message || req.body.text;

  if (!sender) {
    console.error('❌ Missing sender');
    return res.status(400).json({ error: 'Missing sender' });
  }

  if (!message) {
    console.error('❌ Missing message');
    return res.status(400).json({ error: 'Missing message' });
  }

  console.log(`📱 Sender: ${sender}`);
  console.log(`💬 Message: ${message}`);

  try {
    // 1. Parse the SMS to extract transaction ID, amount, and sender
    const parsed = parseSMS(message);
    if (!parsed) {
      console.error('❌ Unrecognized SMS format');
      return res.status(400).json({ error: 'Unrecognized SMS format' });
    }

    const { transactionId, amount } = parsed;
    console.log(`🔑 Transaction ID: ${transactionId}, Amount: ${amount}`);

    // 2. Check if this transaction ID is already used (prevent duplicates)
    const { data: existing, error: checkError } = await supabase
      .from('payments')
      .select('id')
      .eq('transaction_id', transactionId)
      .maybeSingle();

    if (existing) {
      console.warn(`⚠️ Duplicate transaction: ${transactionId}`);
      return res.status(400).json({ error: 'Duplicate transaction' });
    }

    // 3. Find a pending payment matching sender and amount
    const { data: pending, error: findError } = await supabase
      .from('payments')
      .select('*')
      .eq('sender_phone', sender)
      .eq('amount', amount)
      .eq('status', 'pending')
      .order('created_at', { ascending: false })
      .limit(1);

    if (findError) {
      console.error('❌ Database find error:', findError);
      return res.status(500).json({ error: 'Database error' });
    }

    if (!pending || pending.length === 0) {
      console.warn(`❌ No pending payment for sender ${sender}, amount ${amount}`);
      return res.status(404).json({ error: 'No matching pending payment' });
    }

    const payment = pending[0];
    const planConfig = {
      day_pass: 1,
      monthly_vip: 30,
      quarterly_pro: 90
    };

    const durationDays = planConfig[payment.selected_plan] || 30;
    const expiry = new Date(Date.now() + durationDays * 24 * 60 * 60 * 1000);

    // 4. Update payment with transaction ID and mark as paid
    const { error: updatePaymentError } = await supabase
      .from('payments')
      .update({
        status: 'paid',
        transaction_id: transactionId,
        sms_received_at: new Date().toISOString(),
        verified_at: new Date().toISOString(),
        verification_reason: 'SMS received'
      })
      .eq('id', payment.id);

    if (updatePaymentError) {
      console.error('❌ Update payment error:', updatePaymentError);
      return res.status(500).json({ error: 'Failed to update payment' });
    }

    // 5. Update user subscription
    const { error: updateUserError } = await supabase
      .from('users')
      .update({
        subscription_status: 'active',
        subscription_plan: payment.selected_plan,
        subscription_start: new Date().toISOString(),
        subscription_expiry: expiry.toISOString()
      })
      .eq('id', payment.user_id);

    if (updateUserError) {
      console.error('❌ Update user error:', updateUserError);
      return res.status(500).json({ error: 'Failed to update user' });
    }

    console.log(`✅ Subscription activated for user: ${payment.user_id}`);
    console.log(`🔑 Transaction ID: ${transactionId}`);
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

// ------------------- SMS Parser -------------------
function parseSMS(message) {
  // Patterns for MTN and Airtel
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
        // Ref comes first
        transactionId = match[1];
        amount = parseFloat(match[2].replace(/,/g, ''));
        senderNumber = match[3];
      } else {
        // Ref comes last
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
