/**
 * CloudFrontOriginDistribution - single-origin CloudFront distribution
 * fronting an ALB, plus optional cross-region ACM certificate and shared
 * access-log bucket.
 *
 * Translated from:
 *   - terraform/aws-ecs/cloudfront.tf
 *   - terraform/aws-ecs/cloudfront-acm.tf
 *   - terraform/aws-ecs/cloudfront-logging.tf
 *
 * Each caller (Registry-Auth for Keycloak, Registry-Service for the registry
 * ALB) instantiates one of these. Splitting per origin lets each stack own
 * its own CloudFront distribution, which breaks the earlier cross-stack
 * cycle: Auth reads its own distribution's domain to build KC_HOSTNAME
 * without needing a separate CDN stack.
 *
 * No-op when config.cloudfront.enabled is false.
 */

import * as cdk from 'aws-cdk-lib';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as acm from 'aws-cdk-lib/aws-certificatemanager';
import * as route53 from 'aws-cdk-lib/aws-route53';
import * as s3 from 'aws-cdk-lib/aws-s3';
import { Construct } from 'constructs';
import { RegistryConfig } from '../registry-config';

export interface CloudFrontOriginDistributionProps {
  readonly config: RegistryConfig;
  /** DNS name of the ALB origin */
  readonly albDns: string;
  /** Custom domain alias for this distribution (empty when Route53 disabled) */
  readonly customDomain: string;
  /** Route53 hosted zone (used for DNS-validated ACM cert issuance) */
  readonly hostedZone?: route53.IHostedZone;
  /** CloudFront distribution comment */
  readonly comment: string;
  /** S3 log prefix inside the shared access-log bucket */
  readonly logsPrefix: string;
  /** Emit X-Cloudfront-Forwarded-Proto in addition to X-Forwarded-Proto */
  readonly emitCloudFrontForwardedProtoHeader: boolean;
}

export class CloudFrontOriginDistribution extends Construct {
  /** CloudFront distribution (undefined when CloudFront disabled) */
  public readonly distribution?: cloudfront.IDistribution;

  /** Distribution domain name (empty string when CloudFront disabled) */
  public readonly distributionDomainName: string;

  /** Full HTTPS URL to the distribution (empty when CloudFront disabled) */
  public readonly url: string;

  constructor(scope: Construct, id: string, props: CloudFrontOriginDistributionProps) {
    super(scope, id);

    const { config, albDns, customDomain, hostedZone, comment, logsPrefix } = props;

    if (!config.cloudfront.enabled) {
      this.distributionDomainName = '';
      this.url = '';
      return;
    }

    const logsBucket = _getOrCreateLogsBucket(this, config);

    const certificate = _createCrossRegionCert(this, config, hostedZone, customDomain);

    const customHeaders: Record<string, string> = {
      'X-Forwarded-Proto': 'https',
    };
    if (props.emitCloudFrontForwardedProtoHeader) {
      customHeaders['X-Cloudfront-Forwarded-Proto'] = 'https';
    }

    const origin = new origins.HttpOrigin(albDns, {
      protocolPolicy: cloudfront.OriginProtocolPolicy.HTTP_ONLY,
      httpPort: 80,
      httpsPort: 443,
      customHeaders,
    });

    const certificateConfig: {
      certificate?: acm.ICertificate;
      domainNames?: string[];
      sslSupportMethod?: cloudfront.SSLMethod;
      minimumProtocolVersion?: cloudfront.SecurityPolicyProtocol;
    } = {};
    if (certificate && customDomain !== '') {
      certificateConfig.certificate = certificate;
      certificateConfig.domainNames = [customDomain];
      certificateConfig.sslSupportMethod = cloudfront.SSLMethod.SNI;
      certificateConfig.minimumProtocolVersion = cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021;
    }

    const distribution = new cloudfront.Distribution(this, 'Distribution', {
      enabled: true,
      comment,
      priceClass: cloudfront.PriceClass.PRICE_CLASS_100,
      enableLogging: true,
      logBucket: logsBucket,
      logFilePrefix: logsPrefix,
      logIncludesCookies: false,
      defaultBehavior: {
        origin,
        allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
        cachedMethods: cloudfront.CachedMethods.CACHE_GET_HEAD,
        cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
        originRequestPolicy: cloudfront.OriginRequestPolicy.ALL_VIEWER,
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        compress: true,
      },
      ...certificateConfig,
    });

    cdk.Tags.of(distribution).add('Component', id.toLowerCase());

    this.distribution = distribution;
    this.distributionDomainName = distribution.distributionDomainName;
    this.url = `https://${distribution.distributionDomainName}`;
  }
}

/**
 * Create (or reuse) the shared CloudFront access-log bucket for the current
 * stack. Each stack that hosts a CloudFrontOriginDistribution ends up with
 * one bucket; each distribution uses its own logsPrefix.
 */
function _getOrCreateLogsBucket(scope: Construct, config: RegistryConfig): s3.IBucket {
  const stack = cdk.Stack.of(scope);
  const bucketId = 'CloudFrontLogsBucket';
  const existing = stack.node.tryFindChild(bucketId) as s3.Bucket | undefined;
  if (existing) {
    return existing;
  }
  const bucket = new s3.Bucket(stack, bucketId, {
    // Include account ID + stack name because S3 bucket names are globally
    // unique. Stack name suffix disambiguates Auth vs Service log buckets.
    bucketName: `${config.name}-${stack.region}-${stack.account}-${stack.stackName.toLowerCase()}-cflogs`,
    blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
    encryption: s3.BucketEncryption.S3_MANAGED,
    versioned: true,
    objectOwnership: s3.ObjectOwnership.BUCKET_OWNER_PREFERRED,
    removalPolicy: cdk.RemovalPolicy.DESTROY,
    autoDeleteObjects: true,
    lifecycleRules: [
      {
        id: 'delete-old-logs',
        enabled: true,
        expiration: cdk.Duration.days(90),
      },
    ],
  });
  cdk.Tags.of(bucket).add('Purpose', 'CloudFront access logs');
  cdk.Tags.of(bucket).add('Component', 'logging');
  return bucket;
}

/**
 * Create a cross-region ACM certificate in us-east-1 for CloudFront.
 * Only created when both CloudFront and a hostedZone + customDomain are
 * provided. Returns undefined otherwise.
 */
function _createCrossRegionCert(
  scope: Construct,
  config: RegistryConfig,
  hostedZone: route53.IHostedZone | undefined,
  domainName: string,
): acm.ICertificate | undefined {
  if (!config.enableRoute53Dns || !hostedZone || domainName === '') {
    return undefined;
  }
  return new acm.DnsValidatedCertificate(scope, 'Cert', {
    domainName,
    hostedZone,
    region: 'us-east-1',
    cleanupRoute53Records: true,
  });
}
