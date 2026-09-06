#pragma once
#include <stdint.h>
#include "hal.h"   // supplies the TFT_eSPI alias on Cardputer; real class on StickC

// Multi-species ASCII buddy renderer. Each species lives in its own
// src/buddies/<name>.cpp file and exposes 7 state functions matching
// the PersonaState enum order: sleep, idle, busy, attention, celebrate,
// dizzy, heart.
void buddyInit();
void buddyTick(uint8_t personaState);
void buddyInvalidate();
void buddyRenderTo(TFT_eSPI* tgt, uint8_t personaState);
void buddySetSpecies(const char* name);
void buddySetSpeciesIdx(uint8_t idx);
void buddyNextSpecies();
void buddySetPeek(bool peek);
uint8_t buddySpeciesIdx();
uint8_t buddySpeciesCount();
const char* buddySpeciesName();

// Per-species state function: takes the global tickCount and renders
// the buddy + any overlays for the current state into the shared sprite.
typedef void (*StateFn)(uint32_t t);

struct Species {
  const char* name;
  // Declared here, but NOT what the renderer uses: each species passes its
  // colour to buddyPrintSprite at every call site, and nothing reads this
  // field. The two therefore have to be kept in agreement by hand, which is
  // how three species ended up drawing in 0xFFFF — the same white as the
  // text — while claiming a colour here that nobody consulted.
  //
  // Worth collapsing into one source of truth someday. Left alone for now
  // because doing it means touching every call in all twenty species files,
  // and the immediate problem was the colours, not the shape.
  uint16_t bodyColor;
  StateFn states[7];   // index by PersonaState (0=sleep .. 6=heart)
};
