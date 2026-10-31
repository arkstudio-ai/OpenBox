import 'dart:async';

import 'package:flutter/material.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';

class CapabilityCard {
  const CapabilityCard({
    required this.title,
    required this.body,
    required this.icon,
  });

  final String title;
  final String body;
  final IconData icon;
}

/// "等的时候，看看之后还能做什么": three cards on a pager that advances
/// itself every few seconds and stops the moment a finger takes over.
class CapabilityCards extends StatefulWidget {
  const CapabilityCards({
    super.key,
    required this.cards,
    this.interval = const Duration(seconds: 4),
  });

  final List<CapabilityCard> cards;
  final Duration interval;

  @override
  State<CapabilityCards> createState() => _CapabilityCardsState();
}

class _CapabilityCardsState extends State<CapabilityCards> {
  final _pages = PageController(viewportFraction: 0.92);
  Timer? _auto;
  int _index = 0;

  @override
  void initState() {
    super.initState();
    _auto = Timer.periodic(widget.interval, (_) {
      if (!mounted || widget.cards.length < 2 || !_pages.hasClients) return;
      final next = (_index + 1) % widget.cards.length;
      _pages.animateToPage(
        next,
        duration: const Duration(milliseconds: 320),
        curve: Curves.easeOutCubic,
      );
    });
  }

  @override
  void dispose() {
    _auto?.cancel();
    _pages.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        SizedBox(
          height: 96,
          child: Listener(
            onPointerDown: (_) => _auto?.cancel(),
            child: PageView.builder(
              controller: _pages,
              itemCount: widget.cards.length,
              onPageChanged: (i) => setState(() => _index = i),
              itemBuilder: (context, i) {
                final card = widget.cards[i];
                final lead = i == 0;
                return Padding(
                  padding: const EdgeInsets.symmetric(horizontal: 4),
                  child: Container(
                    padding: const EdgeInsets.all(14),
                    decoration: BoxDecoration(
                      color: lead ? t.a700 : t.surface,
                      borderRadius: BorderRadius.circular(Radii.xl),
                    ),
                    child: Row(
                      children: [
                        Icon(card.icon, size: 24, color: lead ? t.bg : t.ink),
                        const SizedBox(width: 12),
                        Expanded(
                          child: Column(
                            mainAxisAlignment: MainAxisAlignment.center,
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              Text(
                                card.title,
                                maxLines: 1,
                                overflow: TextOverflow.ellipsis,
                                style: TextStyle(
                                  fontSize: FontSizes.lg,
                                  fontWeight: FontWeight.w700,
                                  color: lead ? t.bg : t.ink,
                                ),
                              ),
                              const SizedBox(height: 2),
                              Text(
                                card.body,
                                maxLines: 2,
                                overflow: TextOverflow.ellipsis,
                                style: TextStyle(
                                  fontSize: FontSizes.sm,
                                  height: 1.4,
                                  color: lead ? t.a100 : t.n700,
                                ),
                              ),
                            ],
                          ),
                        ),
                      ],
                    ),
                  ),
                );
              },
            ),
          ),
        ),
        const SizedBox(height: 10),
        Row(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            for (var i = 0; i < widget.cards.length; i++)
              AnimatedContainer(
                duration: const Duration(milliseconds: 200),
                margin: const EdgeInsets.symmetric(horizontal: 3),
                width: i == _index ? 18 : 6,
                height: 6,
                decoration: BoxDecoration(
                  color: i == _index ? t.ink : t.n400,
                  borderRadius: BorderRadius.circular(Radii.full),
                ),
              ),
          ],
        ),
      ],
    );
  }
}
