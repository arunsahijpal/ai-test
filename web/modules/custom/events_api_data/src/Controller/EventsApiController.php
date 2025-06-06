<?php

namespace Drupal\events_api_data\Controller;

use Drupal\Core\Controller\ControllerBase;
use Drupal\node\NodeStorageInterface;
use Drupal\Core\Url;
use Symfony\Component\DependencyInjection\ContainerInterface;
use Symfony\Component\HttpFoundation\JsonResponse;
use Symfony\Component\HttpFoundation\Request;
use Drupal\Core\File\FileUrlGeneratorInterface;

/**
 * Provides a controller for the Events API.
 */
class EventsApiController extends ControllerBase {

  /**
   * Node Storage Service.
   *
   * @var \Drupal\node\NodeStorageInterface
   */
  protected $nodeStorage;

  /**
   * File URL generator service.
   *
   * @var \Drupal\Core\File\FileUrlGeneratorInterface
   */
  protected $fileUrlGenerator;

  /**
   * EventsApiController object.
   *
   * @param \Drupal\node\NodeStorageInterface $node_storage
   *   The node storage service.
   * @param \Drupal\Core\File\FileUrlGeneratorInterface $file_url_generator
   *   The file URL generator service.
   */
  public function __construct(NodeStorageInterface $node_storage, FileUrlGeneratorInterface $file_url_generator) {
    $this->nodeStorage = $node_storage;
    $this->fileUrlGenerator = $file_url_generator;
  }

  /**
   * Creates an instance of the EventsApiController.
   *
   * @param \Symfony\Component\DependencyInjection\ContainerInterface $container
   *   The service container.
   *
   * @return static
   *   The instance of controller.
   */
  public static function create(ContainerInterface $container) {
    return new static(
      $container->get('entity_type.manager')->getStorage('node'),
      $container->get('file_url_generator')
    );
  }

  /**
   * Returns events data with JSON response.
   *
   * @param \Symfony\Component\HttpFoundation\Request $request
   *   The request object.
   *
   * @return \Symfony\Component\HttpFoundation\JsonResponse
   *   The JSON response with events data.
   */
  public function getEvents(Request $request) {
    $query = \Drupal::entityQuery('node')
      ->condition('type', 'product')
      ->condition('status', 1)
      ->sort('created', 'DESC')
      ->accessCheck(TRUE);

    $nids = $query->execute();

    $nodes = $this->nodeStorage->loadMultiple($nids);
    $events = [];

    foreach ($nodes as $node) {
      $images = [];
      if ($node->hasField('field_images') && !$node->get('field_images')->isEmpty()) {
        $image_field_items = $node->get('field_images')->referencedEntities();
        foreach ($image_field_items as $image) {
          // Get the image URL using the injected service
          $image_uri = $image->getFileUri();
          $image_url = $this->fileUrlGenerator->generateAbsoluteString($image_uri);
          $images[] = $image_url;
        }
      }

      $events[] = [
        'title' => $node->getTitle(),
        'type' => $node->bundle(),
        'body' => $node->get('body')->value,
        'price' => $node->get('field_price')->value,
        'images' => $images,
      ];
    }

    return new JsonResponse($events);
  }
}
