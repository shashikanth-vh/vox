"""The mailbox reader — opt-in per person.

A person connects their work mailbox once (Google's consent screen, read-only).
Every hour the reader lists what is new in each connected mailbox, keeps the
conversations with an outside party, attaches each to a company, reads its
meaning with the model, and files ONE interaction per conversation into the
Register at the mail's own date. What it cannot attach waits in a review queue.

Modules:
  store    — the reader's memory (connections, filed conversations, the queue),
             tokens encrypted at rest
  google   — OAuth code exchange, token refresh and the Gmail REST calls
  threads  — a Gmail thread as the reader sees it: messages, parties, text, filters
  match    — which company a conversation is about
  extract  — the model call that turns a conversation into a structured note
  reader   — the hourly run, the filing, the queue, the loop
"""
