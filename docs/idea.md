The Loom — a living story the whole room writes at once

A story is projected on the big screen. Anyone in the room scans a QR code and throws in a phrase — any phrase, as weird as they like: "a stolen accordion," "it was Tuesday on Mars," "she never trusted the notary." Each submission gets pinned into the canvas as frozen tokens at a random position — and the story visibly re-denoises around it, reweaving itself into coherence with every new intrusion. Submissions glow in each contributor's color; the connective tissue the model regenerates shimmers as it resolves. The story never restarts and never breaks — it absorbs.

This is the surrealists' Exquisite Corpse — the 1920s parlor game where artists blindly added fragments to a shared work — reborn with an engine that can actually hold it together. That cultural hook gives your pitch a one-line origin story judges can retell to each other, which is half of what "most creative" means.

Why only DiffusionGemma can be this. An autoregressive model handed a mid-story insertion has two options: regenerate everything after it (slow, destroys what people liked) or awkwardly patch forward-only (the text before the insertion can never react to it). Diffusion's bidirectional canvas means a phrase dropped into the middle sends ripples both directions — the story's opening can retroactively plant a setup for the accordion someone just added to the ending. And the ~1100 tok/s speed is what makes it feel live rather than "submit and wait." The mechanism isn't under the hood; it's the show.

Why it wins. The demo isn't something judges watch — it's something they do. The moment a judge's own phrase appears glowing on the projector and the story bends itself around it, you've converted the evaluation into participation; every other team is showing a screen recording while your audience is playing with the exhibit. It's also self-scaling theater: the weirder the audience gets (and hackathon audiences get weird), the better the demo becomes, because absorbing hostile nonsense gracefully is the capability being demonstrated. Close the pitch by scrolling back through the story's revision history — a time-lapse of every ripple — and offer it as a signed artifact: "written by everyone in this room and no one."


VOICES
               🎙️    🎙️    🎙️
                     │
                     ▼
              Live transcription
                     │
                     ▼
             ┌──────────────┐
             │DiffusionGemma│
             │              │
             │ Shared story │
             │ World state  │
             └──────┬───────┘
                    │
          each committed round (~6–15 sec)
                    │
                    ▼
       ┌────────────────────────┐
       │      Nano Banana       │
       │  (Gemini 3.7 Flash)    │
       │                        │
       │ image edit: last still │
       │   + new chunk of text  │
       └───────────┬────────────┘
                   │
        droppable, latest wins — a still
        in flight is dropped if the next
             round commits first
                   │
                   ▼
        crossfade in over ~400ms
                   │
                   ▼

         🖼️ ILLUSTRATED WORLD