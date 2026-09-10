import 'json.dart';

/// Billing wire models. Decimal money/credit values intentionally remain
/// strings so presentation code never changes their value through IEEE-754.
class CreditBalance {
  const CreditBalance({
    required this.workspaceId,
    required this.balance,
    required this.mode,
  });

  factory CreditBalance.fromJson(Map<String, dynamic> json) => CreditBalance(
    workspaceId: asString(json['workspace_id']) ?? '',
    balance: asString(json['balance']) ?? '0',
    mode: asString(json['mode']) ?? 'off',
  );

  final String workspaceId;
  final String balance;
  final String mode;
}

class BillingSummary {
  const BillingSummary({
    required this.totalTokens,
    required this.totalCredits,
    required this.chargedCredits,
    required this.historicalCount,
    required this.unpricedCount,
  });

  factory BillingSummary.fromJson(Map<String, dynamic> json) => BillingSummary(
    totalTokens: asInt(json['total_tokens']) ?? 0,
    totalCredits: asString(json['total_credits']) ?? '0',
    chargedCredits: asString(json['charged_credits']) ?? '0',
    historicalCount: asInt(json['historical_count']) ?? 0,
    unpricedCount: asInt(json['unpriced_count']) ?? 0,
  );

  final int totalTokens;
  final String totalCredits;
  final String chargedCredits;
  final int historicalCount;
  final int unpricedCount;
}

class UsageCredits {
  const UsageCredits({
    this.input = 0,
    this.output = 0,
    this.cache = 0,
    this.durationSec,
    this.minutesBilled,
    this.secondsBilled,
    this.images,
    this.items,
    this.tier,
    this.resolution,
  });

  /// Media events (video_compose / video_generate / video_transcribe /
  /// image_gen) carry quantities instead of tokens; web mirrors this shape.
  factory UsageCredits.fromJson(Map<String, dynamic> json) => UsageCredits(
    input: asInt(json['input']) ?? 0,
    output: asInt(json['output']) ?? 0,
    cache: asInt(json['cache']) ?? asInt(json['cache_read']) ?? 0,
    durationSec: (json['duration_sec'] is num)
        ? (json['duration_sec'] as num).toDouble()
        : null,
    minutesBilled: asInt(json['minutes_billed']),
    secondsBilled: asInt(json['seconds_billed']),
    images: asInt(json['images']),
    items: asInt(json['items']),
    tier: asString(json['tier']),
    resolution: asString(json['resolution']),
  );

  final int input;
  final int output;
  final int cache;
  final double? durationSec;
  final int? minutesBilled;
  final int? secondsBilled;
  final int? images;
  final int? items;
  final String? tier;
  final String? resolution;
}

class UsageEntry {
  const UsageEntry({
    required this.id,
    required this.sessionId,
    required this.sessionTitle,
    required this.sessionAvailable,
    required this.modelId,
    required this.kind,
    required this.tokens,
    required this.totalTokens,
    required this.credits,
    required this.status,
    required this.createdAt,
    required this.pricingVersion,
  });

  factory UsageEntry.fromJson(Map<String, dynamic> json) => UsageEntry(
    id: asString(json['id']) ?? '',
    sessionId: asString(json['session_id']) ?? '',
    sessionTitle: asString(json['session_title']) ?? '',
    sessionAvailable: asBool(json['session_available']) ?? false,
    modelId: asString(json['model_id']) ?? '',
    kind: asString(json['kind']) ?? '',
    tokens: UsageCredits.fromJson(asMap(json['tokens'])),
    totalTokens: asInt(json['total_tokens']) ?? 0,
    credits: asString(json['credits']),
    status: asString(json['status']) ?? 'unpriced',
    createdAt:
        asDate(json['created_at']) ?? DateTime.fromMillisecondsSinceEpoch(0),
    pricingVersion: asString(json['pricing_version']) ?? '',
  );

  final String id;
  final String sessionId;
  final String sessionTitle;
  final bool sessionAvailable;
  final String modelId;
  final String kind;
  final UsageCredits tokens;
  final int totalTokens;
  final String? credits;
  final String status;
  final DateTime createdAt;
  final String pricingVersion;
}

class PagedResult<T> {
  const PagedResult({
    required this.items,
    required this.total,
    required this.page,
    required this.pageSize,
    required this.totalPages,
  });

  factory PagedResult.fromJson(
    Map<String, dynamic> json,
    T Function(Map<String, dynamic>) decode,
  ) => PagedResult(
    items: asList(
      json['items'],
    ).whereType<Map<String, dynamic>>().map(decode).toList(),
    total: asInt(json['total']) ?? 0,
    page: asInt(json['page']) ?? 1,
    pageSize: asInt(json['page_size']) ?? 0,
    totalPages: asInt(json['total_pages']) ?? 1,
  );

  final List<T> items;
  final int total;
  final int page;
  final int pageSize;
  final int totalPages;
}

class PaymentOrder {
  const PaymentOrder({
    required this.id,
    required this.provider,
    required this.credits,
    required this.status,
    required this.checkoutUrl,
    required this.createdAt,
    required this.paidAt,
    required this.cancelledAt,
    required this.cancellationReason,
    required this.reconcileRequired,
    required this.kind,
    required this.amountFen,
    required this.currency,
    required this.planId,
    required this.cycle,
    required this.startsAt,
    required this.endsAt,
  });

  factory PaymentOrder.fromJson(Map<String, dynamic> json) => PaymentOrder(
    id: asString(json['id']) ?? '',
    provider: asString(json['provider']) ?? '',
    credits: asString(json['credits']) ?? '0',
    status: asString(json['status']) ?? 'pending',
    checkoutUrl: asString(json['checkout_url']),
    createdAt:
        asDate(json['created_at']) ?? DateTime.fromMillisecondsSinceEpoch(0),
    paidAt: asDate(json['paid_at']),
    cancelledAt: asDate(json['cancelled_at']),
    cancellationReason: asString(json['cancellation_reason']),
    reconcileRequired: asBool(json['reconcile_required']) ?? false,
    kind: asString(json['kind']) ?? 'topup',
    amountFen: asInt(json['amount_fen']) ?? 0,
    currency: asString(json['currency']) ?? 'CNY',
    planId: asString(json['plan_id']),
    cycle: asString(json['cycle']),
    startsAt: asDate(json['starts_at']),
    endsAt: asDate(json['ends_at']),
  );

  final String id;
  final String provider;
  final String credits;
  final String status;
  final String? checkoutUrl;
  final DateTime createdAt;
  final DateTime? paidAt;
  final DateTime? cancelledAt;
  final String? cancellationReason;
  final bool reconcileRequired;
  final String kind;
  final int amountFen;
  final String currency;
  final String? planId;
  final String? cycle;
  final DateTime? startsAt;
  final DateTime? endsAt;

  bool get pending => status == 'pending';
}

class PaymentProviderInfo {
  const PaymentProviderInfo({
    required this.id,
    required this.name,
    required this.confirmationMode,
    required this.supportsStatusQuery,
    required this.supportsCancel,
    required this.supportsAppCheckout,
    required this.refreshCheckout,
  });

  factory PaymentProviderInfo.fromJson(Map<String, dynamic> json) =>
      PaymentProviderInfo(
        id: asString(json['id']) ?? '',
        name: asString(json['name']) ?? '',
        confirmationMode: asString(json['confirmation_mode']) ?? 'callback',
        supportsStatusQuery: asBool(json['supports_status_query']) ?? false,
        supportsCancel: asBool(json['supports_cancel']) ?? false,
        supportsAppCheckout: asBool(json['supports_app_checkout']) ?? false,
        refreshCheckout: asBool(json['refresh_checkout']) ?? false,
      );

  final String id;
  final String name;
  final String confirmationMode;
  final bool supportsStatusQuery;
  final bool supportsCancel;
  final bool supportsAppCheckout;
  final bool refreshCheckout;
}

class BillingPlan {
  const BillingPlan({
    required this.id,
    required this.monthlyPriceFen,
    required this.yearlyPriceFen,
    required this.credits,
    required this.creditPeriod,
    required this.recommended,
  });

  factory BillingPlan.fromJson(Map<String, dynamic> json) {
    final prices = asMap(json['prices_fen']);
    return BillingPlan(
      id: asString(json['id']) ?? 'free',
      monthlyPriceFen: asInt(prices['monthly']) ?? 0,
      yearlyPriceFen: asInt(prices['yearly']) ?? 0,
      credits: asString(json['credits']) ?? '0',
      creditPeriod: asString(json['credit_period']) ?? 'monthly',
      recommended: asBool(json['recommended']) ?? false,
    );
  }

  final String id;
  final int monthlyPriceFen;
  final int yearlyPriceFen;
  final String credits;
  final String creditPeriod;
  final bool recommended;

  int priceFor(String cycle) =>
      cycle == 'yearly' ? yearlyPriceFen : monthlyPriceFen;
}

class TopupRules {
  const TopupRules({
    required this.minAmountFen,
    required this.maxAmountFen,
    required this.presetsFen,
    required this.creditsPerYuan,
  });

  factory TopupRules.fromJson(Map<String, dynamic> json) => TopupRules(
    minAmountFen: asInt(json['min_amount_fen']) ?? 100,
    maxAmountFen: asInt(json['max_amount_fen']) ?? 10000000,
    presetsFen: asList(
      json['presets_fen'],
    ).map(asInt).whereType<int>().toList(),
    creditsPerYuan: asString(json['credits_per_yuan']) ?? '1',
  );

  final int minAmountFen;
  final int maxAmountFen;
  final List<int> presetsFen;
  final String creditsPerYuan;
}

class BillingPlans {
  const BillingPlans({
    required this.version,
    required this.currency,
    required this.plans,
    required this.topup,
  });

  factory BillingPlans.fromJson(Map<String, dynamic> json) => BillingPlans(
    version: asString(json['version']) ?? '',
    currency: asString(json['currency']) ?? 'CNY',
    plans: asList(
      json['plans'],
    ).whereType<Map<String, dynamic>>().map(BillingPlan.fromJson).toList(),
    topup: TopupRules.fromJson(asMap(json['topup'])),
  );

  final String version;
  final String currency;
  final List<BillingPlan> plans;
  final TopupRules topup;
}

class SubscriptionTerm {
  const SubscriptionTerm({
    required this.planId,
    required this.cycle,
    required this.startsAt,
    required this.endsAt,
  });

  factory SubscriptionTerm.fromJson(Map<String, dynamic> json) =>
      SubscriptionTerm(
        planId: asString(json['plan_id']) ?? 'free',
        cycle: asString(json['cycle']),
        startsAt: asDate(json['starts_at']),
        endsAt: asDate(json['ends_at']),
      );

  final String planId;
  final String? cycle;
  final DateTime? startsAt;
  final DateTime? endsAt;
}

class BillingSubscription extends SubscriptionTerm {
  const BillingSubscription({
    required super.planId,
    required super.cycle,
    required super.startsAt,
    required super.endsAt,
    required this.credits,
    required this.creditPeriod,
    required this.nextGrantAt,
    required this.topupAllowed,
    required this.canManage,
    required this.queued,
  });

  factory BillingSubscription.fromJson(Map<String, dynamic> json) =>
      BillingSubscription(
        planId: asString(json['plan_id']) ?? 'free',
        cycle: asString(json['cycle']),
        startsAt: asDate(json['starts_at']),
        endsAt: asDate(json['ends_at']),
        credits: asString(json['credits']) ?? '0',
        creditPeriod: asString(json['credit_period']) ?? 'monthly',
        nextGrantAt: asDate(json['next_grant_at']),
        topupAllowed: asBool(json['topup_allowed']) ?? false,
        canManage: asBool(json['can_manage']) ?? false,
        queued: asList(json['queued'])
            .whereType<Map<String, dynamic>>()
            .map(SubscriptionTerm.fromJson)
            .toList(),
      );

  final String credits;
  final String creditPeriod;
  final DateTime? nextGrantAt;
  final bool topupAllowed;
  final bool canManage;
  final List<SubscriptionTerm> queued;
}

class NativeAppCheckout {
  const NativeAppCheckout({
    required this.order,
    required this.provider,
    required this.sdkPayload,
  });

  factory NativeAppCheckout.fromJson(Map<String, dynamic> json) =>
      NativeAppCheckout(
        order: PaymentOrder.fromJson(asMap(json['order'])),
        provider: asString(json['provider']) ?? '',
        sdkPayload: asString(json['sdk_payload']),
      );

  final PaymentOrder order;
  final String provider;
  final String? sdkPayload;
}
