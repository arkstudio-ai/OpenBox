import 'json.dart';

/// Mirrors `AuthUser` (frontend-v2 `shared/types/api.ts:297-302`).
class AuthUser {
  const AuthUser({
    required this.id,
    required this.username,
    this.email,
    this.role = 'user',
  });

  factory AuthUser.fromJson(Map<String, dynamic> json) => AuthUser(
        id: asString(json['id']) ?? '',
        username: asString(json['username']) ?? '',
        email: asString(json['email']),
        role: asString(json['role']) ?? 'user',
      );

  final String id;
  final String username;
  final String? email;
  final String role;
}
